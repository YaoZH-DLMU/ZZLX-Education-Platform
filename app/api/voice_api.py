"""
语音输入接口  (Phase Voice)
======================================
POST /api/voice/process
  Body:   { "text": "...", "type": "sign_question|choice|vote|short" }
  将识别好的文字通过 DeepSeek 整理为结构化题目。

POST /api/voice/asr
  Body:   multipart/form-data  audio=<webm/ogg文件>  [stop_word=<停止词>]
  将音频文件发给阿里云 NLS 一句话识别，返回 { "text": "...", "error": null }
  前端停止词检测：返回 text 后由前端自行截断停止词后的内容。

两个接口均需登录（教师端调用）。
"""

import os
import subprocess
from flask import request, jsonify
from flask_login import login_required
from . import api


@api.route('/voice/process', methods=['POST'])
@login_required
def voice_process():
    """将语音转录文本通过 DeepSeek 整理为结构化题目数据。"""
    data  = request.get_json(silent=True) or {}
    text  = str(data.get('text', '')).strip()
    vtype = str(data.get('type', '')).strip()

    if not text:
        return jsonify({'error': '文本不能为空'}), 400
    if vtype not in ('sign_question', 'choice', 'vote', 'short', 'video_comment'):
        return jsonify({'error': f'不支持的类型: {vtype}'}), 400

    from app.utils.ai_client import process_voice_input
    result = process_voice_input(text, vtype)
    return jsonify(result)


@api.route('/voice/asr', methods=['POST'])
@login_required
def voice_asr():
    """
    接收浏览器录制的音频文件，调用阿里云 NLS 一句话识别，返回识别文字。
    前端用 FormData 上传：
        audio : 音频文件 (webm/ogg/wav, ≤60s)
    返回：
        { "text": "识别出的文字", "error": null }
        { "text": "",  "error": "错误信息" }
    """
    if 'audio' not in request.files:
        return jsonify({'text': '', 'error': '未收到音频文件'}), 400

    audio_file = request.files['audio']
    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({'text': '', 'error': '音频文件为空'}), 400

    # 判断格式（浏览器 MediaRecorder 通常输出 webm/ogg）
    content_type = audio_file.content_type or 'audio/webm'
    # 阿里云 NLS 支持 pcm / wav / ogg-opus / mp3 / webm
    # webm(opus) 直接支持，无需转码
    fmt_map = {
        'audio/webm': 'webm',
        'audio/ogg':  'ogg',
        'audio/wav':  'wav',
        'audio/mpeg': 'mp3',
        'audio/mp4':  'mp4',
    }
    audio_format = fmt_map.get(content_type.split(';')[0].strip(), 'webm')

    # 统一转码为 wav(16k,mono)，规避浏览器 webm/ogg 与 NLS 参数不匹配导致的 400
    wav_bytes, convert_error = _convert_to_wav_16k_mono(audio_bytes, audio_format)
    if not wav_bytes:
        return jsonify({'text': '', 'error': f'音频转码失败: {convert_error}'}), 400

    app_key = os.environ.get('ALIYUN_NLS_APPKEY', '')
    if not app_key:
        return jsonify({'text': '', 'error': '服务器未配置阿里云 NLS AppKey'}), 500

    recognized = _call_aliyun_nls(wav_bytes, 'wav', app_key)
    return jsonify(recognized)


def _convert_to_wav_16k_mono(audio_bytes: bytes, audio_format: str):
    """使用 ffmpeg 将输入音频转为 wav 16k 单声道。"""
    fmt = audio_format or 'webm'
    if fmt == 'mp3':
        fmt = 'mp3'
    elif fmt == 'ogg':
        fmt = 'ogg'
    elif fmt == 'wav':
        fmt = 'wav'
    elif fmt == 'webm':
        fmt = 'webm'

    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', fmt,
        '-i', 'pipe:0',
        '-ac', '1',
        '-ar', '16000',
        '-f', 'wav',
        'pipe:1'
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode('utf-8', errors='ignore')[:300]
            return b'', f'ffmpeg返回码={proc.returncode}, {err}'
        return proc.stdout, None
    except Exception as e:
        return b'', f'{type(e).__name__}: {e}'


def _call_aliyun_nls(audio_bytes: bytes, audio_format: str, app_key: str) -> dict:
    """
    调用阿里云 NLS 一句话识别 REST API。
    文档：https://help.aliyun.com/zh/isi/developer-reference/http-based-short-sentence-recognition
    """
    import json as _json
    import urllib.request as _urllib
    import urllib.error as _urlerr

    # 获取 Token（每次调用前获取，有效期12小时，生产可缓存）
    token = _get_aliyun_token()
    if token.get('error'):
        return {'text': '', 'error': 'Token获取失败: ' + token['error']}

    def _post_asr(sample_rate: int) -> dict:
        url = (
            f'https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr'
            f'?appkey={app_key}'
            f'&format={audio_format}'
            f'&sample_rate={sample_rate}'
            f'&enable_punctuation_prediction=true'
            f'&enable_inverse_text_normalization=true'
        )
        req = _urllib.Request(
            url,
            data=audio_bytes,
            headers={
                'X-NLS-Token': token['token'],
                'Content-Type': 'application/octet-stream',
                'Content-Length': str(len(audio_bytes)),
            },
            method='POST',
        )
        with _urllib.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode('utf-8'))

    sample_rates = [16000]
    if audio_format in ('webm', 'ogg'):
        sample_rates = [16000, 48000]

    last_error = ''
    for sr in sample_rates:
        try:
            body = _post_asr(sr)
            if body.get('status') == 20000000:
                return {'text': body.get('result', ''), 'error': None}
            last_error = f"NLS错误 status={body.get('status')} message={body.get('message','')} format={audio_format} sample_rate={sr}"
        except _urlerr.HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='ignore')
            except Exception:
                err_body = ''
            last_error = f'HTTPError {e.code} format={audio_format} sample_rate={sr} body={err_body}'
        except Exception as e:
            last_error = f'{type(e).__name__}: {e} format={audio_format} sample_rate={sr}'

    return {'text': '', 'error': 'NLS调用失败: ' + last_error}


# ── Token 缓存（有效期12小时，避免每次都请求）─────────────────
_nls_token_cache = {'token': '', 'expire': 0}

def _get_aliyun_token() -> dict:
    """获取阿里云 NLS Token，带本地缓存。"""
    import time, json as _json, urllib.request as _urllib

    now = time.time()
    if _nls_token_cache['token'] and now < _nls_token_cache['expire'] - 60:
        return {'token': _nls_token_cache['token'], 'error': None}

    ak_id     = os.environ.get('ALIYUN_ACCESS_KEY_ID', '')
    ak_secret = os.environ.get('ALIYUN_ACCESS_KEY_SECRET', '')

    if not ak_id or not ak_secret:
        # 没有 AK/SK 时，尝试用 ECS 实例 RAM 角色自动获取临时凭证
        try:
            meta_url = 'http://100.100.100.200/latest/meta-data/ram/security-credentials/'
            with _urllib.urlopen(meta_url, timeout=3) as r:
                role = r.read().decode().strip().splitlines()[0]
            cred_url = f'http://100.100.100.200/latest/meta-data/ram/security-credentials/{role}'
            with _urllib.urlopen(cred_url, timeout=3) as r:
                cred = _json.loads(r.read())
            ak_id     = cred['AccessKeyId']
            ak_secret = cred['AccessKeySecret']
            token_val = cred.get('SecurityToken', '')
            # ECS RAM 角色凭证直接包含 token（临时 STS token）
            # NLS token 仍需单独申请，但这里我们用 AK 申请
        except Exception:
            return {'token': '', 'error': '未配置 ALIYUN_ACCESS_KEY_ID/SECRET，且无法获取实例 RAM 角色'}

    # 调用 nls-meta 申请 NLS Token
    try:
        import hmac, hashlib, base64, uuid
        from datetime import datetime, timezone

        def _percent_encode(s):
            import urllib.parse
            return urllib.parse.quote(str(s), safe='')

        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        nonce = str(uuid.uuid4())
        params = {
            'AccessKeyId':      ak_id,
            'Action':           'CreateToken',
            'Format':           'JSON',
            'RegionId':         'cn-shanghai',
            'SignatureMethod':  'HMAC-SHA1',
            'SignatureNonce':   nonce,
            'SignatureVersion': '1.0',
            'Timestamp':        timestamp,
            'Version':          '2019-02-28',
        }
        sorted_params = sorted(params.items())
        canon = '&'.join(f'{_percent_encode(k)}={_percent_encode(v)}' for k, v in sorted_params)
        string_to_sign = f'GET&{_percent_encode("/")}&{_percent_encode(canon)}'
        digest = hmac.new((ak_secret + '&').encode('utf-8'),
                          string_to_sign.encode('utf-8'),
                          hashlib.sha1).digest()
        signature = base64.b64encode(digest).decode()
        params['Signature'] = signature

        import urllib.parse
        query = urllib.parse.urlencode(params)
        url = f'https://nls-meta.cn-shanghai.aliyuncs.com/?{query}'
        with _urllib.urlopen(url, timeout=10) as r:
            data = _json.loads(r.read())

        t = data.get('Token', {})
        tok = t.get('Id', '')
        exp = t.get('ExpireTime', now + 43200)
        _nls_token_cache['token']  = tok
        _nls_token_cache['expire'] = float(exp)
        return {'token': tok, 'error': None}
    except Exception as e:
        return {'token': '', 'error': str(e)}
