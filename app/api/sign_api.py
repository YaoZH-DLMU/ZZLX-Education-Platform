"""
词云签到 API  (Phase 5 - 6-1)
======================================
POST   /api/sign/session              创建签到会话（教师）
GET    /api/sign/sessions             获取本教师的所有会话
PUT    /api/sign/session/<id>/toggle  切换会话开/关
DELETE /api/sign/session/<id>         删除会话
GET    /api/sign/<token>/words        获取词频数据（词云轮询用）
GET    /api/sign/<token>/info         获取会话信息（学生页用）
POST   /api/sign/<token>/respond      学生提交回答（无需登录）
GET    /api/sign/<token>/qr.png       生成二维码图片
"""

from flask import request, jsonify, current_app, send_file
from flask_login import login_required, current_user
from app import db
from app.models import SignSession, SignResponse, HtmlInteraction, PointLog, User
from app.utils.student_list import validate_student_id, load_student_list
from . import api
import io, re, os, base64
from collections import Counter


def _count_suspicious(responses):
    """返回该 session 中「同一设备同一IP提交多个学号」的回答数（即可疑回答条数）。
    
    仅凭指纹相同会产生大量假阳性（同型号手机指纹相同但IP不同）。
    改为要求 device_fp + ip_addr 同时相同，才认为是同一台物理设备在替签。
    """
    key_to_sids = {}
    for r in responses:
        if not r.device_fp:
            continue
        key = (r.device_fp, r.ip_addr or '')
        key_to_sids.setdefault(key, set()).add(r.student_id)
    # 同一(指纹+IP)对应 >1 个学号 → 这些学号的回答打上可疑标记
    suspicious_sids = set()
    for sids in key_to_sids.values():
        if len(sids) > 1:
            suspicious_sids.update(sids)
    return sum(1 for r in responses if r.student_id in suspicious_sids)
from functools import wraps
from datetime import datetime


# ── 教师权限检查 ──────────────────────────────────────────
def teacher_required_api(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or \
                not (current_user.is_teacher or current_user.is_adv_test):
            return jsonify({'error': '仅教师账号可用'}), 403
        return f(*args, **kwargs)
    return decorated


def _sign_adv_test_readonly(f):
    """阻止高级测试账号执行写入/删除/导出操作"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.is_authenticated and current_user.is_adv_test:
            return jsonify({'error': '观察账号不可执行此操作'}), 403
        return f(*args, **kwargs)
    return decorated


# ── 工具：文本预处理 ──────────────────────────────────────
_STRIP_RE = re.compile(r'[\s\u3000\u00a0]+')

def _clean(text: str) -> str:
    """去除首尾空白，压缩内部空格，截断到30字"""
    t = _STRIP_RE.sub(' ', text).strip()
    return t[:30] if t else ''


# ── 教师：创建会话 ─────────────────────────────────────────
@api.route('/sign/session', methods=['POST'])
@login_required
@teacher_required_api
@_sign_adv_test_readonly
def create_sign_session():
    data     = request.get_json(silent=True) or {}
    question = data.get('question', '请用一个词描述今天的学习收获').strip() or '请用一个词描述今天的学习收获'
    sess     = SignSession(question=question[:500], created_by=current_user.id)
    db.session.add(sess)
    db.session.commit()
    return jsonify({'id': sess.id, 'token': sess.token, 'question': sess.question,
                    'is_active': sess.is_active,
                    'created_at': sess.created_at.strftime('%Y-%m-%d %H:%M')}), 201


# ── 教师：列出自己的所有会话 ──────────────────────────────
@api.route('/sign/sessions', methods=['GET'])
@login_required
@teacher_required_api
def list_sign_sessions():
    if current_user.is_adv_test:
        sessions = SignSession.query.order_by(SignSession.created_at.desc()).all()
    else:
        sessions = SignSession.query.filter_by(created_by=current_user.id)\
                                    .order_by(SignSession.created_at.desc()).all()
    return jsonify([{
        'id':         s.id,
        'token':      s.token,
        'question':   s.question,
        'is_active':  s.is_active,
        'count':      len(s.responses),
        'suspicious': _count_suspicious(s.responses),
        'created_at': s.created_at.strftime('%Y-%m-%d %H:%M'),
    } for s in sessions])


# ── 教师：切换会话激活状态 ────────────────────────────────
@api.route('/sign/session/<int:sid>/toggle', methods=['PUT'])
@login_required
@teacher_required_api
@_sign_adv_test_readonly
def toggle_sign_session(sid):
    sess = SignSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    was_active = sess.is_active
    sess.is_active = not sess.is_active
    db.session.commit()
    # 关闭签到时（active→false）在服务端直接导出，不依赖客户端
    if was_active and not sess.is_active:
        _do_export_sign(sess)
    return jsonify({'id': sess.id, 'is_active': sess.is_active})


# ── 教师：删除会话 ────────────────────────────────────────
@api.route('/sign/session/<int:sid>', methods=['DELETE'])
@login_required
@teacher_required_api
@_sign_adv_test_readonly
def delete_sign_session(sid):
    sess = SignSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    db.session.delete(sess)
    db.session.commit()
    return jsonify({'ok': True})


# ── 公共：获取会话信息（学生端用） ───────────────────────
@api.route('/sign/<token>/info', methods=['GET'])
def sign_session_info(token):
    sess = SignSession.query.filter_by(token=token).first_or_404()
    return jsonify({
        'id':        sess.id,
        'question':  sess.question,
        'is_active': sess.is_active,
    })


# ── 公共：获取词频（词云轮询） ────────────────────────────
@api.route('/sign/<token>/words', methods=['GET'])
def sign_words(token):
    sess = SignSession.query.filter_by(token=token).first_or_404()
    # 将每条回答作为一个词单元统计频次
    answers = [_clean(r.answer) for r in sess.responses if r.answer.strip()]
    freq    = Counter(answers)
    # 返回 [[词, 权重], ...] 格式，按频次降序
    words   = [[w, c] for w, c in freq.most_common(80) if w]
    return jsonify({
        'words':     words,
        'total':     len(sess.responses),
        'is_active': sess.is_active,
        'question':  sess.question,
    })


# ── 公共：学生提交答案（无需登录） ───────────────────────
@api.route('/sign/<token>/respond', methods=['POST'])
def sign_respond(token):
    sess = SignSession.query.filter_by(token=token).first_or_404()
    if not sess.is_active:
        return jsonify({'error': '该签到已结束，不再接受提交'}), 403

    data       = request.get_json(silent=True) or {}
    student_id = str(data.get('student_id', '')).strip()
    answer     = _clean(str(data.get('answer', '')))
    device_fp  = str(data.get('device_fp', '') or '')[:64] or None

    if not student_id:
        return jsonify({'error': '请填写学号'}), 400
    if not answer:
        return jsonify({'error': '回答内容不能为空'}), 400

    # 学号名单校验（名单文件存在时生效）
    valid, msg = validate_student_id(student_id)
    if not valid:
        return jsonify({'error': msg}), 400

    # 检查是否已提交过
    existing = SignResponse.query.filter_by(
        session_id=sess.id, student_id=student_id).first()
    if existing:
        return jsonify({'error': '你已参与过本次签到', 'already': True}), 409

    ip  = (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '').split(',')[0].strip()[:45]
    ua  = (request.headers.get('User-Agent', '') or '')[:120]
    resp = SignResponse(session_id=sess.id, student_id=student_id, answer=answer,
                        ip_addr=ip or None, ua_hint=ua or None, device_fp=device_fp)
    db.session.add(resp)
    db.session.commit()
    # 写 cookie 供 PPT 学生端记录 HTML 互动页参与（无需再输学号）；24h 有效
    jr = jsonify({'ok': True, 'total': len(sess.responses)})
    jr.set_cookie('sign_sid', student_id, max_age=24*3600, httponly=True, samesite='Lax')
    return jr


# ── 公共：生成二维码图片 ──────────────────────────────────
@api.route('/sign/<token>/qr.png', methods=['GET'])
@login_required
def sign_qr(token):
    import qrcode
    from qrcode.image.pure import PyPNGImage

    sess = SignSession.query.filter_by(token=token).first_or_404()

    # 使用 url_for 生成外部链接，ProxyFix 会自动加上 /ZZLX 前缀
    from flask import url_for
    url  = url_for('main.sign_respond_page', token=sess.token, _external=True)

    qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                         box_size=10, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=PyPNGImage)

    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype='image/png', download_name='sign_qr.png')


# ── 内部导出逻辑（服务端主动调用，不依赖客户端）──────────
def _award_signin_points(sess):
    """签到关闭时：每个已签到学生 +0.5（reason=sign_in），幂等。"""
    pts = 0.5
    for resp in sess.responses:
        user = User.query.filter_by(student_id=resp.student_id).first()
        if not user:
            continue
        already = PointLog.query.filter_by(
            user_id=user.id, reason='sign_in', ref_session_id=sess.id).first()
        if already:
            continue
        user.reward_points = (user.reward_points or 0) + pts
        db.session.add(PointLog(
            user_id=user.id, points=pts, reason='sign_in', ref_session_id=sess.id,
            memo=f'签到参与 sign#{sess.id}',
        ))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def _award_html_interaction_points(sess):
    """签到关闭时：给已签到学生按其操作过的不同 HTML 互动页数 ×0.2 发分。
    未签到学生无 cookie 不记录，自然不计；幂等（同签到会话不重复发）。"""
    now = datetime.now()
    pts_per = 0.2
    summary = []   # 供报告附注
    for resp in sess.responses:
        sid = resp.student_id
        # 该学号在签到时间窗内操作过的不同 HTML 页数（按 课件+slide 去重）
        distinct_slides = db.session.query(
            HtmlInteraction.ppt_session_id, HtmlInteraction.slide_path
        ).filter(
            HtmlInteraction.student_id == sid,
            HtmlInteraction.created_at >= sess.created_at,
            HtmlInteraction.created_at <= now,
        ).distinct().count()
        if distinct_slides <= 0:
            continue
        user = User.query.filter_by(student_id=sid).first()
        if not user:
            summary.append(f'- {sid}：{distinct_slides} 页（未注册账号，未发分）')
            continue
        # 幂等：该签到会话该学生已发过 html_interact 则跳过
        already = PointLog.query.filter_by(
            user_id=user.id, reason='html_interact', ref_session_id=sess.id
        ).first()
        if already:
            continue
        pts = round(distinct_slides * pts_per, 2)
        user.reward_points = (user.reward_points or 0) + pts
        db.session.add(PointLog(
            user_id=user.id, points=pts, reason='html_interact',
            ref_session_id=sess.id,
            memo=f'HTML互动页参与 {distinct_slides} 页 ×0.2 sign#{sess.id}',
        ))
        summary.append(f'- {sid}  {user.username}：{distinct_slides} 页 +{pts}')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return summary


def _do_export_sign(sess, png_b64=''):
    """将签到数据导出为 Markdown 报告（+可选词云 PNG）到 exports/sign/"""
    now    = datetime.now()
    stamp  = now.strftime('%Y%m%d%H%M')
    safe_q = re.sub(r'[\\/:*?"<>|\r\n]', '', sess.question)[:40]
    basename = f'{stamp}_{safe_q}'

    from flask import current_app
    from app.utils.course_paths import exports_dir
    export_dir = exports_dir('sign')
    os.makedirs(export_dir, exist_ok=True)

    # ── 保存 PNG（若客户端传来）──────────────────────────
    if png_b64:
        if ',' in png_b64:
            png_b64 = png_b64.split(',', 1)[1]
        try:
            png_bytes = base64.b64decode(png_b64)
            with open(os.path.join(export_dir, basename + '.png'), 'wb') as f:
                f.write(png_bytes)
        except Exception:
            pass

    # ── 生成 Markdown 报告 ───────────────────────────────
    responses = sess.responses
    answers   = [r.answer for r in responses]
    sids      = [r.student_id for r in responses]
    freq      = Counter(answers)
    student_list = load_student_list()

    lines = [
        f'# 词云签到报告',
        f'',
        f'**问题：** {sess.question}',
        f'**导出时间：** {now.strftime("%Y-%m-%d %H:%M")}',
        f'**参与人数：** {len(sids)}',
        f'',
        f'## 词云关键词（按频次降序）',
        f'',
    ]
    for w, c in freq.most_common():
        lines.append(f'- {w}（{c}次）')

    lines += ['', '## 参与签到学号', '']
    for sid in sorted(sids):
        name = student_list.get(sid, '（未录入名单）')
        lines.append(f'- {sid}  {name}')

    if student_list:
        absent = sorted(set(student_list.keys()) - set(sids))
        lines += ['', f'## 未签到学号（共{len(absent)}人）', '']
        for sid in absent:
            lines.append(f'- {sid}  {student_list[sid]}')

    # 签到参与积分 +0.5/人，HTML 互动页参与 +0.2/页（仅已签到学生）
    _award_signin_points(sess)
    html_summary = _award_html_interaction_points(sess)
    if html_summary:
        lines += ['', '## HTML 互动页参与积分（+0.2/页）', '']
        lines += html_summary

    md_path = os.path.join(export_dir, basename + '.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return basename


# ── 教师：关闭签到后自动导出词云截图 + MD报告 ─────────────
@api.route('/sign/<token>/export', methods=['POST'])
@login_required
@_sign_adv_test_readonly
def export_sign_session(token):
    """
    前端在签到 is_active 变为 False 时自动调用（补充词云 PNG）。
    服务端已在 toggle 时生成 MD 报告，此处仅补存 PNG 截图。
    """
    sess    = SignSession.query.filter_by(token=token).first_or_404()
    data    = request.get_json(silent=True) or {}
    png_b64 = data.get('png', '')
    basename = _do_export_sign(sess, png_b64)
    return jsonify({'ok': True, 'filename': basename})


# ── 教师：获取情感变化曲线数据 ────────────────────────────
@api.route('/teacher/emotion_curve', methods=['GET'])
@login_required
@teacher_required_api
def get_emotion_curve():
    """返回全部有效签到节点的四维情感分数+摘要，供前端绘制曲线。"""
    from app.utils.emotion_curve import load_curve
    nodes = load_curve()
    return jsonify({'nodes': nodes})


# ── 教师：列出全部有效签到（供管理面板勾选/排除） ──────────
@api.route('/teacher/emotion_curve/signins', methods=['GET'])
@login_required
@teacher_required_api
def list_emotion_signins():
    """扫描全部有效签到 md，返回列表（日期+题目+md_file），供管理面板展示。"""
    import os, glob
    from app.utils.emotion_score import parse_md, count_real_students, is_course_related
    from app.utils.course_paths import exports_dir

    md_dir = exports_dir('sign')
    mds = sorted(glob.glob(os.path.join(md_dir, '*.md')))

    candidates = {}
    for md in mds:
        parsed = parse_md(md)
        if not parsed:
            continue
        if count_real_students(parsed.get('student_ids', [])) < 10:
            continue
        if not is_course_related(parsed['question']):
            continue
        title = parsed['question'][:60]
        date = parsed.get('export_time', '')[:10]
        if title not in candidates or date >= candidates[title][0]:
            candidates[title] = (date, parsed['question'][:80], os.path.basename(md))

    signins = [{'date': d, 'title': t, 'md_file': f}
               for _, (d, t, f) in sorted(candidates.items(), key=lambda x: x[1][0])]
    return jsonify({'signins': signins})


# ── 教师：全量分析情感曲线（一次AI调用打分全部签到） ──────
@api.route('/teacher/emotion_curve/analyze', methods=['POST'])
@login_required
@teacher_required_api
def analyze_emotion_curve():
    """扫描全部有效签到 md（可排除指定 md_file）-> 一次 DeepSeek 打分 -> 保存 -> 返回。"""
    import os, glob
    from app.utils.emotion_score import parse_md, count_real_students, is_course_related, _clean_keyword, _is_noise
    from app.utils.emotion_curve import save_curve
    from app.utils.ai_client import score_emotion_batch

    data = request.get_json(silent=True) or {}
    excluded = set(data.get('excluded', []))  # md_file 列表

    from app.utils.course_paths import exports_dir
    md_dir = exports_dir('sign')
    mds = sorted(glob.glob(os.path.join(md_dir, '*.md')))

    # 解析+去重+过滤+排除
    candidates = {}
    for md in mds:
        md_file = os.path.basename(md)
        if md_file in excluded:
            continue
        parsed = parse_md(md)
        if not parsed:
            continue
        real = count_real_students(parsed.get('student_ids', []))
        if real < 10:
            continue
        if not is_course_related(parsed['question']):
            continue
        title = parsed['question'][:60]
        date = parsed.get('export_time', '')[:10]
        if title not in candidates or date >= candidates[title][0]:
            candidates[title] = (date, parsed, md_file)

    # 按日期排序，准备 AI 输入
    signins = []
    for title, (date, parsed, md_file) in sorted(candidates.items(), key=lambda x: x[1][0]):
        cleaned = []
        for kw in parsed['keywords']:
            w = _clean_keyword(kw['text'])
            if not _is_noise(w):
                cleaned.append({'text': w, 'count': kw['count']})
        signins.append({
            'date': date,
            'title': parsed['question'][:60],
            'question': parsed['question'],
            'participants': parsed['participants'],
            'keywords': cleaned,
            'md_file': md_file,
        })

    if not signins:
        return jsonify({'error': '未找到有效签到数据（需≥10名真实学生+课程相关话题）'}), 400

    # 一次 AI 调用打分全部
    results = score_emotion_batch(signins)
    if not results:
        return jsonify({'error': 'AI分析失败，请稍后重试'}), 500

    # 组装节点
    nodes = []
    for i, si in enumerate(signins):
        r = results[i] if i < len(results) else {}
        nodes.append({
            'date': si['date'],
            'title': si['title'],
            'participants': si['participants'],
            'acceptance': r.get('acceptance', 50),
            'interest': r.get('interest', 50),
            'burden': r.get('burden', 50),
            'autonomy': r.get('autonomy', 50),
            'summary': r.get('summary', ''),
            'tags': r.get('tags', []),
            'confidence': 'ai_batch',
            'md_file': si['md_file'],
            'method_version': 'emotion-curve-v3',
        })

    save_curve(nodes)
    return jsonify({'nodes': nodes, 'count': len(nodes)})
