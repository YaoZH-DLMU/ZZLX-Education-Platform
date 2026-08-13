"""临时诊断脚本：测试 DashScope ASR 密钥是否可用"""
import sys, os
from pathlib import Path

# 读取密钥
key = Path('/app/AliyunKey.txt').read_text(encoding='utf-8').strip()
print(f'Key length: {len(key)}')
print(f'Key prefix: {key[:8]}')

if not key:
    print('ERROR: AliyunKey.txt is empty')
    sys.exit(1)

# 测试 dashscope import
try:
    import dashscope
    dashscope.api_key = key
    from dashscope.audio.asr import Recognition
    print('dashscope import: OK')
except ImportError as e:
    print(f'dashscope import FAILED: {e}')
    sys.exit(1)

# 找一个已有的 WAV 文件测试（或用已有 mp4 提取一小段）
import glob, subprocess, tempfile

mp4s = sorted(glob.glob('/app/app/static/uploads/**/*.mp4', recursive=True))
print(f'MP4 files found: {len(mp4s)}')
if not mp4s:
    print('No MP4 to test with')
    sys.exit(0)

# 取第一个 mp4 提取前 5 秒
test_mp4 = mp4s[0]
wav_path = '/tmp/test_asr_sample.wav'
print(f'Test file: {test_mp4}')
r = subprocess.run(
    ['ffmpeg', '-i', test_mp4, '-t', '5',
     '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', wav_path],
    capture_output=True, timeout=30
)
if r.returncode != 0 or not os.path.exists(wav_path):
    print('FFmpeg WAV extraction FAILED:', r.stderr.decode(errors='replace')[-300:])
    sys.exit(1)
print(f'WAV extracted: {os.path.getsize(wav_path)} bytes')

# 调用 DashScope ASR
print('Calling DashScope ASR...')
try:
    recognition = Recognition(
        model='paraformer-realtime-v2',
        format='wav',
        sample_rate=16000,
        callback=None,
    )
    resp = recognition.call(wav_path)
    print(f'Status code: {resp.status_code}')
    if resp.status_code == 200:
        sentences = (resp.output or {}).get('sentence', [])
        text = ' '.join(s.get('text', '') for s in sentences).strip()
        print(f'Transcript: {text[:200]}')
    else:
        print(f'ASR FAILED: {resp.status_code}')
        print(f'Message: {getattr(resp, "message", resp)}')
        print(f'Output: {resp.output}')
except Exception as e:
    print(f'ASR Exception: {e}')
finally:
    if os.path.exists(wav_path):
        os.remove(wav_path)
