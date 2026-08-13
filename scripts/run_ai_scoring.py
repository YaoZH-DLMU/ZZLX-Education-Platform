"""
run_ai_scoring.py — 对所有尚未 AI 评分的作业视频单独执行打分，不触发压缩。
用法：docker exec zzlxweb-web-1 python /app/scripts/run_ai_scoring.py [--limit N] [--rescore]

选项：
    --limit N   最多处理 N 个视频（默认不限制，处理全部未打分视频）
    --rescore   重新计算已有 AI 分数并覆盖原记录
"""
import os, sys, subprocess, json, re
from pathlib import Path

sys.path.insert(0, '/app')

UPLOAD_FOLDER = '/app/app/static/uploads'

LIMIT = None
RESCORE = '--rescore' in sys.argv
for i, arg in enumerate(sys.argv[1:]):
    if arg == '--limit' and i + 2 <= len(sys.argv) - 1:
        LIMIT = int(sys.argv[i + 2])


def _get_dashscope_key() -> str:
    key = os.environ.get('ALIYUN_DASHSCOPE_KEY', '').strip()
    if key:
        return key
    key_file = Path('/app/AliyunKey.txt')
    if key_file.exists():
        return key_file.read_text(encoding='utf-8').strip()
    return ''


def _load_kp_map() -> dict:
    qg_path = Path('/app/QG.json')
    if not qg_path.exists():
        return {}
    with qg_path.open(encoding='utf-8') as f:
        root = json.load(f)
    kp_map = {}
    for chap in root.get('children', []):
        m = re.search(r'\d+', chap.get('id', ''))
        if not m:
            continue
        ch = int(m.group())
        q_map = {}
        for section in chap.get('children', []):
            label = str(section.get('label', '')).strip()
            sm = re.search(r'(\d+)\s*[-－]\s*(\d+)', label)
            if not sm:
                sm = re.search(r'^(\d+)\D+(\d+)$', label)
            if not sm:
                continue
            section_ch = int(sm.group(1))
            problem_no = int(sm.group(2))
            if section_ch != ch:
                continue
            q_map[problem_no] = [
                kp.get('label', '').strip()
                for kp in section.get('children', [])
                if kp.get('level') == 3 and kp.get('label', '').strip()
            ]
        kp_map[ch] = q_map
    return kp_map


KP_MAP = _load_kp_map()

_SCORE_PROMPT = """你是一位大学力学课程的视频作业评审专家。请只做结构化判定，不要自己计算总分。

【视频信息】
- 标题：{title}
- 章节：第{chapter}章
- 题号：第{problem_no}题
- 是否命中题目图谱：{has_exact_match}

【本题对应知识点】
{kp_lines}

【视频讲解文字转写】
{transcript}

【判定规则】
1. 你只输出 JSON，不要输出任何额外说明。
2. expression_score 只能是 0、0.5、1、1.5、2 之一。
3. 每个知识点的 score 只能是 0、0.5、1 之一。
4. 0 分：未提及，也没有相关推导痕迹。
5. 0.5 分：疑似提及；或被 ASR 谐音/错词污染但上下文可判断；或属于公式/方程/字母符号类知识点，虽然名称没转对，但出现了明显代号、代入、变形、单位或计算链条。
6. 1 分：明确提及且讲清楚了作用、条件、公式意义或使用过程。
7. 如果没有命中题目图谱，请返回空的 kp_scores 列表，并只根据讲解完整度、逻辑性、术语准确度给 expression_score 与 overall_reason。
8. overall_reason 控制在 40 字以内。

请严格输出如下 JSON：
{{
  "expression_score": 0,
  "overall_reason": "",
  "kp_scores": [
    {{"kp": "知识点名", "score": 0, "evidence": "不超过18字"}}
  ]
}}"""


def _parse_problem_no(title: str) -> int | None:
    if not title:
        return None
    patterns = [
        r'第\s*(\d+)\s*章\s*第?\s*(\d+)\s*题',
        r'(\d+)\s*章\s*(\d+)\s*题',
        r'(\d+)\s*[-－]\s*(\d+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, title)
        if m:
            return int(m.group(2))
    return None


def _resolve_problem_no(video) -> int | None:
    if getattr(video, 'problem_no', None):
        return int(video.problem_no)
    return _parse_problem_no(getattr(video, 'title', '') or '')


def _get_problem_kps(chapter: int | None, problem_no: int | None) -> list[str]:
    if not chapter or not problem_no:
        return []
    return KP_MAP.get(chapter, {}).get(problem_no, [])


def _json_block(text: str) -> str | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith('{') and text.endswith('}'):
        return text
    m = re.search(r'\{[\s\S]*\}', text)
    return m.group(0) if m else None


def _clamp_half(value: float, low: float, high: float) -> float:
    value = max(low, min(high, value))
    return round(value * 2) / 2


def _calc_final_score(expression_score: float, kp_scores: list[dict], exact_match: bool) -> float:
    expression_score = _clamp_half(expression_score, 0.0, 2.0)
    if exact_match and kp_scores:
        kp_total = sum(_clamp_half(float(item.get('score', 0) or 0), 0.0, 1.0) for item in kp_scores)
        kp_component = 3.0 * (kp_total / len(kp_scores))
    else:
        kp_component = 0.0
    score = 5.0 + expression_score + kp_component
    score = max(6.0, min(10.0, score))
    return round(score * 2) / 2


def _fallback_score(transcript: str) -> tuple[float, str, list[dict]]:
    length = len((transcript or '').strip())
    if length >= 900:
        expression_score = 2.0
        reason = '讲解完整度较高，但未命中题目图谱'
    elif length >= 500:
        expression_score = 2.0
        reason = '讲解较完整，但未命中题目图谱'
    elif length >= 180:
        expression_score = 1.5
        reason = '讲解基本完整，但未命中题目图谱'
    else:
        expression_score = 1.0
        reason = '讲解较简略，且未命中题目图谱'
    score = max(6.0, min(7.5, round((5.0 + expression_score) * 2) / 2))
    return score, reason, []


def extract_wav(video_path: str) -> str | None:
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


def transcribe(wav_path: str) -> str | None:
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


def ai_score(video, transcript: str) -> tuple:
    chapter = getattr(video, 'chapter', None)
    problem_no = _resolve_problem_no(video)
    kps = _get_problem_kps(chapter, problem_no)
    has_exact_match = bool(kps)
    kp_lines = '\n'.join(f'- {idx}. {kp}' for idx, kp in enumerate(kps, 1)) if kps else '（未命中题目图谱）'
    try:
        from openai import OpenAI
        api_key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
        if not api_key:
            kf = Path('/app/APIKey.txt')
            api_key = kf.read_text(encoding='utf-8').strip() if kf.exists() else ''
        if not api_key:
            print('    [AI Score] 未配置 DEEPSEEK_API_KEY', flush=True)
            return None, '', []
        client = OpenAI(api_key=api_key, base_url='https://api.deepseek.com')
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': _SCORE_PROMPT.format(
                title=video.title,
                chapter=chapter or '?',
                problem_no=problem_no or '?',
                has_exact_match='是' if has_exact_match else '否',
                kp_lines=kp_lines,
                transcript=transcript[:4000])}],
            temperature=0,
            max_tokens=700,
        )
        text = resp.choices[0].message.content.strip()
        raw_json = _json_block(text)
        if not raw_json:
            print(f'    [AI Score] 解析输出失败: {text[:120]}', flush=True)
            if not has_exact_match:
                return _fallback_score(transcript)
            return None, '', []

        data = json.loads(raw_json)
        expression_score = float(data.get('expression_score', 0) or 0)
        reason = str(data.get('overall_reason', '')).strip()[:40]
        kp_scores = []
        expected = set(kps)
        for item in data.get('kp_scores', []) or []:
            kp = str(item.get('kp', '')).strip()
            if kp not in expected:
                continue
            kp_scores.append({
                'kp': kp,
                'score': _clamp_half(float(item.get('score', 0) or 0), 0.0, 1.0),
                'evidence': str(item.get('evidence', '')).strip()[:18],
            })

        if has_exact_match:
            by_name = {item['kp']: item for item in kp_scores}
            kp_scores = [by_name.get(kp, {'kp': kp, 'score': 0.0, 'evidence': ''}) for kp in kps]
            score = _calc_final_score(expression_score, kp_scores, exact_match=True)
        else:
            score, fallback_reason, kp_scores = _fallback_score(transcript)
            if not reason:
                reason = fallback_reason

        if not reason:
            reason = '结构化评分完成'
        return score, reason, kp_scores
    except Exception as e:
        print(f'    [AI Score] 异常: {e}', flush=True)
    if not has_exact_match:
        return _fallback_score(transcript)
    return None, '', []


def main():
    from app import create_app, db
    from app.models import Video, VideoAIScore

    flask_app = create_app()
    with flask_app.app_context():
        existing_scores = {r.video_id: r for r in VideoAIScore.query.all()}
        base_query = Video.query.filter(Video.type == 'homework').order_by(Video.id)
        if RESCORE:
            unscored = base_query.all()
        else:
            scored_ids = set(existing_scores)
            unscored = base_query.filter(
                ~Video.id.in_(scored_ids) if scored_ids else db.true()
            ).all()

        # 过滤出本地文件存在的视频
        to_score = []
        for v in unscored:
            basename = os.path.basename(v.url)
            path = os.path.join(UPLOAD_FOLDER, basename)
            if os.path.exists(path):
                to_score.append((v, path))

        if LIMIT:
            to_score = to_score[:LIMIT]

        total = len(to_score)
        print(f'共 {total} 个视频待评分\n', flush=True)

        ok = fail = skip = 0
        for idx, (video, path) in enumerate(to_score, 1):
            from app.models import User as _User
            uname = _User.query.get(video.user_id).username if video.user_id else '?'
            print(f'[{idx}/{total}] {video.title}  (作者: {uname})', flush=True)
            wav_path = extract_wav(path)
            if not wav_path:
                print('  跳过（WAV 提取失败）\n', flush=True)
                fail += 1
                continue
            try:
                print('  正在转写语音...', flush=True)
                transcript = transcribe(wav_path)
                if not transcript:
                    print('  跳过（转写为空）\n', flush=True)
                    skip += 1
                    continue
                print(f'  转写完成（{len(transcript)} 字符）', flush=True)

                print('  正在 AI 评分...', flush=True)
                score, reason, kp_scores = ai_score(video, transcript)
                if score is None:
                    print('  跳过（AI 评分失败）\n', flush=True)
                    fail += 1
                    continue

                rec = existing_scores.get(video.id)
                if rec is None:
                    rec = VideoAIScore(video_id=video.id)
                    db.session.add(rec)
                rec.score = score
                rec.transcript = transcript
                rec.kp_matched = json.dumps(kp_scores, ensure_ascii=False)
                rec.reason = reason
                db.session.commit()
                matched = sum(1 for item in kp_scores if float(item.get('score', 0) or 0) > 0)
                total_kp = len(kp_scores)
                extra = f'（知识点 {matched}/{total_kp}）' if total_kp else '（未命中图谱）'
                print(f'  ✅ {score} 分 {extra} — {reason}\n', flush=True)
                ok += 1
            finally:
                if os.path.exists(wav_path):
                    os.remove(wav_path)

        print(f'\n=== 完成 ===  成功: {ok}  失败: {fail}  跳过: {skip}  共: {total}')


if __name__ == '__main__':
    main()
