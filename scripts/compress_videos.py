"""
compress_videos.py — 压缩上传目录中超过 20MB 的视频，并触发 AI 评分
用法：docker exec zzlxweb-web-1 python /app/scripts/compress_videos.py [--dry-run] [--all] [--no-ai]

选项：
  --dry-run   只列出需要压缩的文件，不实际压缩
  --all       处理所有历史大文件（默认只处理近 24h 内上传的新文件）
  --no-ai     跳过 AI 评分步骤（仅压缩）

注意：AI 评分默认只对尚未评分的视频执行，不会重复打分；仅 run_ai_scoring.py --rescore 才会覆盖历史分数。
"""
import os, sys, subprocess, glob, json, re
from pathlib import Path
from datetime import datetime, timedelta

# 确保可以导入 Flask 应用
sys.path.insert(0, '/app')

UPLOAD_FOLDER = '/app/app/static/uploads'
SIZE_LIMIT_MB = 20
DRY_RUN    = '--dry-run' in sys.argv
ALL_FILES  = '--all'     in sys.argv   # 不加此参数则默认只处理近 24h 内的新文件
NO_AI      = '--no-ai'   in sys.argv

# ── 阿里云 ASR 密钥读取 ─────────────────────────────────────────

def _get_dashscope_key() -> str:
    key = os.environ.get('ALIYUN_DASHSCOPE_KEY', '').strip()
    if key:
        return key
    key_file = Path('/app/AliyunKey.txt')
    if key_file.exists():
        return key_file.read_text(encoding='utf-8').strip()
    return ''

# ── 音频提取 ───────────────────────────────────────────────────

def extract_wav(video_path: str) -> str | None:
    """从视频提取 16k 单声道 WAV，成功返回临时文件路径，失败返回 None"""
    wav_path = video_path + '.tmp_asr.wav'
    try:
        r = subprocess.run(
            ['ffmpeg', '-i', video_path,
             '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
             '-y', wav_path],
            capture_output=True, timeout=120
        )
        if r.returncode == 0 and os.path.exists(wav_path):
            return wav_path
    except Exception as e:
        print(f'    [ASR] WAV 提取失败: {e}', flush=True)
    return None


def get_duration(video_path: str) -> float | None:
    """用 ffprobe 取视频时长（秒），失败返回 None"""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True, timeout=30
        )
        if r.returncode == 0:
            return float(r.stdout.decode().strip())
    except Exception:
        pass
    return None


def pick_checkpoint(duration: float) -> float | None:
    """在时长 10%-90% 之间取一个随机检查点（秒）；时长不足 20s 返回 None"""
    if not duration or duration < 20:
        return None
    import random
    return round(random.uniform(0.1 * duration, 0.9 * duration), 2)


def write_transcript_file(video, ai_rec):
    """写一份 json 转译文件到 app/exports/video_transcripts/<video_id>.json，方便检索"""
    try:
        d = os.path.join(current_app_root(), 'exports', 'video_transcripts')
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f'{video.id}.json')
        with open(fp, 'w', encoding='utf-8') as f:
            json.dump({
                'video_id': video.id,
                'title': video.title,
                'duration': ai_rec.duration_sec,
                'checkpoint': ai_rec.checkpoint_sec,
                'score': ai_rec.score,
                'transcript': ai_rec.transcript or '',
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'    [AI] 转译文件写入失败: {e}', flush=True)


def current_app_root():
    from flask import current_app
    try:
        return current_app.root_path
    except Exception:
        return '/app/app'


# ── 阿里云 DashScope ASR ────────────────────────────────────────

def transcribe(wav_path: str) -> str | None:
    """调用阿里云 Paraformer 转写 WAV，返回文本；失败返回 None"""
    api_key = _get_dashscope_key()
    if not api_key:
        print('    [ASR] 未配置 ALIYUN_DASHSCOPE_KEY，跳过转写', flush=True)
        return None
    try:
        import dashscope
        from dashscope.audio.asr import Recognition
        dashscope.api_key = api_key
        recognition = Recognition(
            model='paraformer-realtime-v2',
            format='wav',
            sample_rate=16000,
            callback=None,
        )
        # 直接传文件路径（DashScope SDK 支持本地绝对路径）
        resp = recognition.call(wav_path)
        if resp.status_code == 200:
            sentences = (resp.output or {}).get('sentence', [])
            return ' '.join(s.get('text', '') for s in sentences).strip()
        else:
            print(f'    [ASR] 请求失败: {resp.status_code} {getattr(resp, "message", resp)}', flush=True)
    except ImportError:
        print('    [ASR] 缺少 dashscope 包，请 pip install dashscope', flush=True)
    except Exception as e:
        print(f'    [ASR] 异常: {e}', flush=True)
    return None

# ── 视频压缩 ────────────────────────────────────────────────────

def is_10bit(path):
    """检测视频是否为10-bit色深（yuv420p10le等），此类视频手机浏览器无法播放"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=pix_fmt', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=15
        )
        pix = result.stdout.strip()
        return pix.endswith('10le') or pix.endswith('10be') or '10' in pix
    except Exception:
        return False

def compress(path):
    tmp = path + '.compressing.mp4'
    orig_mb = os.path.getsize(path) / (1024 * 1024)
    print(f'  压缩中... {os.path.basename(path)}  ({orig_mb:.1f} MB)', flush=True)
    try:
        result = subprocess.run(
            ['ffmpeg', '-i', path,
             '-c:v', 'libx264', '-crf', '28', '-preset', 'medium',
             '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
             '-threads', '2', '-y', tmp],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0 and os.path.exists(tmp):
            new_mb = os.path.getsize(tmp) / (1024 * 1024)
            os.replace(tmp, path)
            print(f'  ✅ 完成  {orig_mb:.1f} MB -> {new_mb:.1f} MB  '
                  f'(节省 {orig_mb - new_mb:.1f} MB, {(1 - new_mb/orig_mb)*100:.0f}%)', flush=True)
            return True
        else:
            print(f'  ❌ 失败 returncode={result.returncode}', flush=True)
            print(f'     stderr: {result.stderr[-300:]}', flush=True)
            return False
    except subprocess.TimeoutExpired:
        print(f'  ❌ 超时（> 600s）', flush=True)
        return False
    except Exception as e:
        print(f'  ❌ 异常: {e}', flush=True)
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

# ── AI 评分主流程（含 Flask app context）──────────────────────

def run_ai_scoring_for_video(path: str):
    """压缩成功后，对视频运行完整的 AI 评分流水线"""
    basename = os.path.basename(path)
    # 通过 URL 字段找到对应的 DB 记录
    # Video.url 格式：/static/uploads/xxx.mp4
    url_suffix = path.replace('/app/app', '')  # → /static/uploads/xxx.mp4

    try:
        from app import create_app, db
        from app.models import Video, VideoAIScore
        from scripts.run_ai_scoring import ai_score as structured_ai_score
        flask_app = create_app()
        with flask_app.app_context():
            video = Video.query.filter(Video.url.like(f'%{basename}')).first()
            if not video:
                print(f'  [AI] 找不到视频 DB 记录（basename={basename}），跳过', flush=True)
                return
            if video.type != 'homework':
                print(f'  [AI] 非作业视频（type={video.type}），跳过评分', flush=True)
                return
            existing = VideoAIScore.query.filter_by(video_id=video.id).first()
            if existing:
                # 已评分但缺检查点（旧记录）-> 补检查点 + 写转译文件
                if existing.checkpoint_sec is None:
                    dur = get_duration(path)
                    cp = pick_checkpoint(dur) if dur else None
                    if cp:
                        existing.duration_sec = dur
                        existing.checkpoint_sec = cp
                        db.session.commit()
                        write_transcript_file(video, existing)
                        print(f'  [AI] 补检查点 {cp}s/{dur}s', flush=True)
                print(f'  [AI] 已有 AI 评分 ({existing.score} 分)，跳过', flush=True)
                return

            print(f'  [AI] 开始 AI 评分流水线 → {video.title}', flush=True)

            # 1. 提取 WAV
            wav_path = extract_wav(path)
            if not wav_path:
                print('  [AI] WAV 提取失败，跳过', flush=True)
                return

            try:
                # 2. ASR 转写
                print('  [AI] 正在转写语音...', flush=True)
                transcript = transcribe(wav_path)
                if not transcript:
                    print('  [AI] 转写失败或为空，跳过', flush=True)
                    return
                print(f'  [AI] 转写完成，{len(transcript)} 字符', flush=True)

                # 3. DeepSeek 评分
                print('  [AI] 正在 AI 评分...', flush=True)
                score, reason, kp_scores = structured_ai_score(video, transcript)
                if score is None:
                    print('  [AI] 评分失败，跳过', flush=True)
                    return

                # 3.5 检查点：取时长 + 随机检查点（10%-90%）
                duration = get_duration(path)
                checkpoint = pick_checkpoint(duration) if duration else None

                # 4. 保存
                ai_rec = VideoAIScore(
                    video_id   = video.id,
                    score      = score,
                    transcript = transcript,
                    kp_matched = json.dumps(kp_scores, ensure_ascii=False),
                    reason     = reason,
                    duration_sec   = duration,
                    checkpoint_sec = checkpoint,
                )
                db.session.add(ai_rec)
                db.session.commit()
                write_transcript_file(video, ai_rec)
                matched = sum(1 for item in kp_scores if float(item.get('score', 0) or 0) > 0)
                total_kp = len(kp_scores)
                extra = f'（知识点 {matched}/{total_kp}）' if total_kp else '（未命中图谱）'
                print(f'  [AI] ✅ 评分完成：{score} 分 {extra} — {reason}', flush=True)
            finally:
                if os.path.exists(wav_path):
                    os.remove(wav_path)
    except Exception as e:
        print(f'  [AI] 流水线异常: {e}', flush=True)

# ── 主逻辑 ──────────────────────────────────────────────────────

files = sorted(
    glob.glob(os.path.join(UPLOAD_FOLDER, '*.mp4'))
    + glob.glob(os.path.join(UPLOAD_FOLDER, '*.MP4'))
    + glob.glob(os.path.join(UPLOAD_FOLDER, '*.mov'))
    + glob.glob(os.path.join(UPLOAD_FOLDER, '*.MOV'))
)

big_files = [(p, os.path.getsize(p) / (1024*1024)) for p in files
             if os.path.getsize(p) / (1024*1024) > SIZE_LIMIT_MB or is_10bit(p)]

if not ALL_FILES:
    # 默认只处理近 24 小时内上传的新文件（按文件修改时间），避免重复压缩
    now = datetime.now()
    since_ts = (now - timedelta(hours=24)).timestamp()
    big_files = [(p, mb) for p, mb in big_files if os.path.getmtime(p) >= since_ts]
    print(f'（默认模式：仅处理近 24h 内上传的大文件，传 --all 可处理全部历史文件）')
else:
    print(f'（--all 模式：处理所有历史大文件）')

print(f'上传目录：{UPLOAD_FOLDER}')
print(f'超过 {SIZE_LIMIT_MB} MB 的文件：{len(big_files)} 个\n')
for p, mb in big_files:
    print(f'  {mb:6.1f} MB  {os.path.basename(p)}')

if DRY_RUN:
    print('\n（dry-run 模式，不执行压缩）')
    sys.exit(0)

if not big_files:
    print('\n无需压缩（近 24h 内无新的大文件）。')
    # 注意：不 exit，继续执行下方的 AI 评分扫描
else:
    print('\n开始压缩（可随时 Ctrl+C 中断，已完成的不会回退）...\n')
    ok = fail = 0
    compressed_paths = []
    for p, mb in big_files:
        print(f'\n[{big_files.index((p,mb))+1}/{len(big_files)}] {os.path.basename(p)}')
        if compress(p):
            ok += 1
            compressed_paths.append(p)
        else:
            fail += 1

    print(f'\n完成：成功 {ok} 个，失败 {fail} 个')

    # ── AI 评分（压缩成功的文件）────────────────────────────────────
    if not NO_AI and compressed_paths:
        print(f'\n开始 AI 评分（压缩后，共 {len(compressed_paths)} 个视频）...\n')
        for p in compressed_paths:
            print(f'\n▶ {os.path.basename(p)}')
            run_ai_scoring_for_video(p)
        print('\n压缩视频 AI 评分完成。')

# ── AI 评分（所有尚未评分的作业视频，含小文件）────────────────────
if not NO_AI:
    print('\n扫描所有未评分的作业视频（含无需压缩的小文件）...')
    try:
        from app import create_app, db
        from app.models import Video, VideoAIScore
        flask_app = create_app()
        with flask_app.app_context():
            scored_ids = {r.video_id for r in VideoAIScore.query.all()}
            unscored = Video.query.filter(
                Video.type == 'homework',
                ~Video.id.in_(scored_ids) if scored_ids else db.true()
            ).all()

            # 过滤出路径存在的本地文件
            to_score = []
            for v in unscored:
                basename = os.path.basename(v.url)
                path = os.path.join(UPLOAD_FOLDER, basename)
                if os.path.exists(path) and path not in compressed_paths:
                    to_score.append(path)

        if to_score:
            print(f'发现 {len(to_score)} 个未评分的小/新视频，开始 AI 评分...\n')
            for p in to_score:
                print(f'\n▶ {os.path.basename(p)}')
                run_ai_scoring_for_video(p)
            print('\n小视频 AI 评分完成。')
        else:
            print('所有作业视频均已评分，无需补充。')
    except Exception as e:
        print(f'扫描未评分视频时异常: {e}')

