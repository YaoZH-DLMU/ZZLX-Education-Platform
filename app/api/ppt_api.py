"""
PPT 课堂 API  (Phase 5 - 6-2)
=========================================================
教师（需登录 + 教师权限）：
  POST   /api/ppt/session                   创建课件会话
  GET    /api/ppt/sessions                  列出自己所有课件
  DELETE /api/ppt/session/<sid>             删除课件会话
  POST   /api/ppt/session/<sid>/slide       上传幻灯片
  DELETE /api/ppt/session/<sid>/slide/<idx> 删除幻灯片
  PUT    /api/ppt/session/<sid>/slide/move  上移/下移 {idx, dir:'up'|'down'}
  PUT    /api/ppt/<token>/slide             翻页 {delta:±1} or {index:N}
  POST   /api/ppt/<token>/interact          开始互动 {type:'choice'|'vote'|'short'}
  DELETE /api/ppt/<token>/interact          关闭当前互动

公共：
  GET    /api/ppt/<token>/state             学生轮询状态
  GET    /api/ppt/<token>/results           互动结果轮询（含弹幕）
  POST   /api/ppt/<token>/respond           学生提交回答
  GET    /api/ppt/<token>/qr.png            二维码图片
"""

import os, json, time, io, re
from datetime import datetime
from flask import request, jsonify, current_app, send_file
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from functools import wraps
from app import db
from app.models import PptSession, PptInteraction, PptResponse, HtmlInteraction
from app.utils.student_list import validate_student_id
from . import api

ALLOWED_SLIDE = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'mov'}

def _log_dir():
    from flask import current_app
    # 写入 app/ 目录内，该目录在 docker-compose 已挂载到宿主机 /opt/ZZLXWeb/app/
    from app.utils.course_paths import exports_dir
    d = exports_dir('ppt')
    os.makedirs(d, exist_ok=True)
    return d


def _log_path(token: str) -> str:
    """按日期+token 生成确定性路径，多 worker 下各进程均可找到同一文件。"""
    stamp = datetime.now().strftime('%Y%m%d')
    return os.path.join(_log_dir(), f'{stamp}_{token}.md')


def _append_log(token: str, line: str):
    path = _log_path(token)
    if os.path.exists(path):
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


def _teacher_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': '仅教师账号可用'}), 403
        if not current_user.is_teacher and not current_user.is_adv_test:
            return jsonify({'error': '仅教师账号可用'}), 403
        return f(*args, **kwargs)
    return decorated


def _adv_test_readonly(f):
    """高级测试账号只能只读，不能写入/修改/删除任何数据"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.is_authenticated and current_user.is_adv_test:
            return jsonify({'error': '测试账号不支持此操作'}), 403
        return f(*args, **kwargs)
    return decorated


def _slide_dir(token):
    from app.utils.course_paths import upload_folder as _uf
    return os.path.join(_uf(), 'ppt', token)


def _slide_url(sess, path):
    """生成幻灯片访问 URL（按课程上传目录动态拼接）。"""
    from app.utils.course_paths import upload_folder as _uf
    from flask import current_app
    # upload_folder() 返回绝对路径如 /app/app/static/uploads 或 /app/app/static/uploads/lllx
    # 需要转为 /static/uploads 或 /static/uploads/lllx
    static_dir = os.path.join(current_app.root_path, 'static')
    rel = os.path.relpath(_uf(), static_dir)
    return f'/static/{rel}/ppt/{sess.token}/{path}'


def _slides(sess):
    try:
        return json.loads(sess.slides_json or '[]')
    except Exception:
        return []


def _save_slides(sess, slides):
    sess.slides_json = json.dumps(slides, ensure_ascii=False)


# ── 创建课件会话 ─────────────────────────────────────────
@api.route('/ppt/session', methods=['POST'])
@login_required
@_teacher_required
@_adv_test_readonly
def create_ppt_session():
    data  = request.get_json(silent=True) or {}
    title = (data.get('title') or '课堂课件').strip()[:200]
    group = (data.get('group_label') or '默认').strip()[:50] or '默认'
    sess  = PptSession(title=title, group_label=group, created_by=current_user.id)
    db.session.add(sess)
    db.session.commit()
    os.makedirs(_slide_dir(sess.token), exist_ok=True)
    return jsonify({'id': sess.id, 'token': sess.token, 'title': sess.title,
                    'group_label': sess.group_label or '默认',
                    'slide_count': 0, 'slides': [],
                    'created_at': sess.created_at.strftime('%Y-%m-%d %H:%M')}), 201


# ── 列出课件 ─────────────────────────────────────────────
@api.route('/ppt/sessions', methods=['GET'])
@login_required
@_teacher_required
def list_ppt_sessions():
    # 高级测试账号可以看到所有人的课件
    if current_user.is_adv_test:
        sessions = PptSession.query.order_by(PptSession.created_at.desc()).all()
    else:
        sessions = PptSession.query.filter_by(created_by=current_user.id) \
                                   .order_by(PptSession.created_at.desc()).all()
    return jsonify([{
        'id':                s.id,
        'token':             s.token,
        'title':             s.title,
        'group_label':       s.group_label or '默认',
        'slide_count':       len(_slides(s)),
        'slides':            _slides(s),
        'is_shared':         bool(s.is_shared),
        'interaction_count': PptInteraction.query.filter_by(session_id=s.id).count(),
        'blocked_count':     PptInteraction.query.filter_by(session_id=s.id, is_blocked=True).count(),
        'created_at':        s.created_at.strftime('%Y-%m-%d %H:%M'),
    } for s in sessions])


# ── 互动列表 / 屏蔽（管理界面用） ─────────────────
def _session_owned(sid):
    """返回当前用户拥有的 PptSession（高级测试可看全部），无则 404。"""
    if current_user.is_adv_test:
        return PptSession.query.get_or_404(sid)
    return PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()


@api.route('/ppt/session/<int:sid>/interactions', methods=['GET'])
@login_required
@_teacher_required
def list_ppt_interactions(sid):
    """列出某课件全部互动（含屏蔽状态、回答数），供管理界面悬浮窗展示。"""
    _session_owned(sid)
    items = PptInteraction.query.filter_by(session_id=sid) \
                .order_by(PptInteraction.created_at.asc()).all()
    out = []
    for ia in items:
        try:
            options = json.loads(ia.options_json) if ia.options_json else []
        except Exception:
            options = []
        out.append({
            'id':             ia.id,
            'itype':          ia.itype,
            'question':       ia.question or '',
            'options':        options,
            'is_active':      bool(ia.is_active),
            'is_blocked':     bool(ia.is_blocked),
            'response_count': PptResponse.query.filter_by(interaction_id=ia.id).count(),
            'created_at':     ia.created_at.strftime('%Y-%m-%d %H:%M') if ia.created_at else '',
        })
    return jsonify(out)


@api.route('/ppt/session/<int:sid>/interaction/<int:ia_id>/block', methods=['PUT'])
@login_required
@_teacher_required
@_adv_test_readonly
def toggle_ppt_interaction_block(sid, ia_id):
    """切换某互动的屏蔽状态。屏蔽后该互动不参与结束积分召回。"""
    _session_owned(sid)
    ia = PptInteraction.query.filter_by(id=ia_id, session_id=sid).first_or_404()
    data = request.get_json(silent=True) or {}
    ia.is_blocked = bool(data.get('blocked', not ia.is_blocked))
    db.session.commit()
    return jsonify({'success': True, 'is_blocked': bool(ia.is_blocked)})


@api.route('/ppt/session/<int:sid>/interaction/<int:ia_id>', methods=['DELETE'])
@login_required
@_teacher_required
@_adv_test_readonly
def delete_ppt_interaction(sid, ia_id):
    """删除一个互动及其回答（级联删 PptResponse）。不回退已发放的积分（PointLog 留作历史）。"""
    _session_owned(sid)
    ia = PptInteraction.query.filter_by(id=ia_id, session_id=sid).first_or_404()
    db.session.delete(ia)      # cascade='all, delete-orphan' 自动删回答
    db.session.commit()
    return jsonify({'success': True})


# ── 切换分享状态 ────────────────────────────────
@api.route('/ppt/session/<int:sid>/share', methods=['PUT'])
@login_required
@_teacher_required
@_adv_test_readonly
def toggle_ppt_share(sid):
    """Toggle is_shared — 帮帮学生展示或隐藏某个课件"""
    sess = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    sess.is_shared = not sess.is_shared
    db.session.commit()
    return jsonify({'ok': True, 'is_shared': sess.is_shared})


# ── 学生：查看已分享课件列表 ─────────────────────
@api.route('/ppt/shared_sessions', methods=['GET'])
@login_required
def list_shared_ppt_sessions():
    """Returns all PPT sessions marked is_shared=True"""
    sessions = PptSession.query.filter_by(is_shared=True) \
                               .order_by(PptSession.created_at.desc()).all()
    return jsonify([{
        'id':          s.id,
        'token':       s.token,
        'title':       s.title,
        'group_label': s.group_label or '默认',
        'slide_count': len(_slides(s)),
        'created_at':  s.created_at.strftime('%Y-%m-%d %H:%M'),
    } for s in sessions])


# ── 学生：获取单个已分享课件状态 ────────────────
@api.route('/ppt/shared/<token>/state', methods=['GET'])
@login_required
def get_shared_ppt_state(token):
    """Student-facing state for a shared PPT (no interactions exposed)"""
    sess = PptSession.query.filter_by(token=token, is_shared=True).first_or_404()
    slides = _slides(sess)
    return jsonify({
        'title':      sess.title,
        'slides':     [{
            'type': s['type'],
            'url':  _slide_url(sess, s["path"]),
            'name': s.get('name', ''),
        } for s in slides],
        'total':      len(slides),
    })


# ── 删除课件会话 ─────────────────────────────────────────
@api.route('/ppt/session/<int:sid>', methods=['DELETE'])
@login_required
@_teacher_required
@_adv_test_readonly
def delete_ppt_session(sid):
    sess = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    db.session.delete(sess)
    db.session.commit()
    return jsonify({'ok': True})


# ── 修改课件所属分组 ──────────────────────────────────────
@api.route('/ppt/session/<int:sid>/group', methods=['PUT'])
@login_required
@_teacher_required
@_adv_test_readonly
def update_ppt_session_group(sid):
    sess = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    data = request.get_json(silent=True) or {}
    group = (data.get('group_label') or '默认').strip()[:50] or '默认'
    sess.group_label = group
    db.session.commit()
    return jsonify({'ok': True, 'group_label': sess.group_label})


# ── 上传幻灯片 ───────────────────────────────────────────
@api.route('/ppt/session/<int:sid>/slide', methods=['POST'])
@login_required
@_teacher_required
@_adv_test_readonly
def upload_ppt_slide(sid):
    sess = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    if 'file' not in request.files:
        return jsonify({'error': '没有文件字段'}), 400
    f   = request.files['file']
    ext = f.filename.rsplit('.', 1)[-1].lower() if f.filename and '.' in f.filename else ''
    if ext not in ALLOWED_SLIDE:
        return jsonify({'error': f'不支持 .{ext} 格式'}), 400
    sdir  = _slide_dir(sess.token)
    os.makedirs(sdir, exist_ok=True)
    fname = secure_filename(f'{int(time.time() * 1000)}_{f.filename}')
    f.save(os.path.join(sdir, fname))
    ftype  = 'video' if ext in {'mp4', 'webm', 'mov'} else 'image'
    slides = _slides(sess)
    slides.append({'type': ftype, 'path': fname, 'name': f.filename})
    _save_slides(sess, slides)
    db.session.commit()
    return jsonify({'ok': True, 'slides': slides}), 201


# ── 保存 HTML 代码幻灯片 ────────────────────────────────
@api.route('/ppt/session/<int:sid>/slide/html', methods=['POST'])
@login_required
@_teacher_required
@_adv_test_readonly
def upload_ppt_slide_html(sid):
    sess = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    data = request.get_json(silent=True) or {}
    html_code = str(data.get('html_code', '')).strip()
    if not html_code:
        return jsonify({'error': 'html_code 不能为空'}), 400
    if len(html_code) > 2 * 1024 * 1024:   # 2 MB 上限
        return jsonify({'error': 'HTML 代码不能超过 2 MB'}), 400
    sdir  = _slide_dir(sess.token)
    os.makedirs(sdir, exist_ok=True)
    fname = f'{int(time.time() * 1000)}_html_slide.html'
    fpath = os.path.join(sdir, fname)
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(html_code)
    slides = _slides(sess)
    slides.append({'type': 'html', 'path': fname, 'name': 'HTML页面'})
    _save_slides(sess, slides)
    db.session.commit()
    return jsonify({'ok': True, 'slides': slides}), 201


# ── 删除某张幻灯片 ───────────────────────────────────────
@api.route('/ppt/session/<int:sid>/slide/<int:idx>', methods=['DELETE'])
@login_required
@_teacher_required
@_adv_test_readonly
def delete_ppt_slide(sid, idx):
    sess   = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    slides = _slides(sess)
    if idx < 0 or idx >= len(slides):
        return jsonify({'error': '索引越界'}), 400
    removed = slides.pop(idx)
    try:
        os.remove(os.path.join(_slide_dir(sess.token), removed['path']))
    except OSError:
        pass
    if sess.current_slide >= len(slides) and slides:
        sess.current_slide = len(slides) - 1
    _save_slides(sess, slides)
    db.session.commit()
    return jsonify({'ok': True, 'slides': slides})


# ── 幻灯片上移/下移 ──────────────────────────────────────
@api.route('/ppt/session/<int:sid>/slide/move', methods=['PUT'])
@login_required
@_teacher_required
@_adv_test_readonly
def move_ppt_slide(sid):
    sess   = PptSession.query.filter_by(id=sid, created_by=current_user.id).first_or_404()
    data   = request.get_json(silent=True) or {}
    try:
        idx = int(data.get('idx', -1))
    except (TypeError, ValueError):
        return jsonify({'error': '无效索引'}), 400
    dirn   = data.get('dir', '')
    slides = _slides(sess)
    if idx < 0 or idx >= len(slides):
        return jsonify({'error': '索引越界'}), 400

    def _move_to(target_idx):
        target_idx = max(0, min(len(slides) - 1, target_idx))
        if target_idx == idx:
            return
        moved_slide = slides.pop(idx)
        slides.insert(target_idx, moved_slide)

        current_idx = sess.current_slide or 0
        if current_idx == idx:
            sess.current_slide = target_idx
        elif idx < current_idx <= target_idx:
            sess.current_slide = current_idx - 1
        elif target_idx <= current_idx < idx:
            sess.current_slide = current_idx + 1

    if data.get('target_index', None) not in (None, ''):
        try:
            target_idx = int(data.get('target_index'))
        except (TypeError, ValueError):
            return jsonify({'error': '目标位置无效'}), 400
        _move_to(target_idx)
    elif dirn == 'up' and idx > 0:
        _move_to(idx - 1)
    elif dirn == 'down' and idx < len(slides) - 1:
        _move_to(idx + 1)
    _save_slides(sess, slides)
    db.session.commit()
    return jsonify({'ok': True, 'slides': slides})


# ── 教师翻页 ─────────────────────────────────────────────
@api.route('/ppt/<token>/slide', methods=['PUT'])
@login_required
@_teacher_required
@_adv_test_readonly
def set_ppt_slide(token):
    sess   = PptSession.query.filter_by(token=token, created_by=current_user.id).first_or_404()
    data   = request.get_json(silent=True) or {}
    slides = _slides(sess)
    total  = len(slides)
    if total == 0:
        return jsonify({'error': '无幻灯片'}), 400
    if 'delta' in data:
        sess.current_slide = max(0, min(total - 1, sess.current_slide + int(data['delta'])))
    elif 'index' in data:
        sess.current_slide = max(0, min(total - 1, int(data['index'])))
    db.session.commit()
    return jsonify({'current': sess.current_slide, 'total': total})


# ── 开始互动 ─────────────────────────────────────────────
@api.route('/ppt/<token>/interact', methods=['POST'])
@login_required
@_teacher_required
@_adv_test_readonly
def start_ppt_interact(token):
    sess     = PptSession.query.filter_by(token=token, created_by=current_user.id).first_or_404()
    data     = request.get_json(silent=True) or {}
    itype    = data.get('type', '')
    if itype not in ('choice', 'vote', 'short'):
        return jsonify({'error': '无效互动类型'}), 400
    question = str(data.get('question', '') or '').strip()[:200] or None
    options  = data.get('options', [])   # list[str]
    opts_json = json.dumps(options, ensure_ascii=False) if options else None
    PptInteraction.query.filter_by(session_id=sess.id, is_active=True) \
                        .update({'is_active': False})
    ia = PptInteraction(session_id=sess.id, itype=itype,
                        question=question, options_json=opts_json)
    db.session.add(ia)
    db.session.commit()
    return jsonify({'id': ia.id, 'type': ia.itype,
                    'question': ia.question,
                    'options':  json.loads(ia.options_json) if ia.options_json else []}), 201


# ── 关闭互动 ─────────────────────────────────────────────
@api.route('/ppt/<token>/interact', methods=['DELETE'])
@login_required
@_teacher_required
@_adv_test_readonly
def stop_ppt_interact(token):
    sess = PptSession.query.filter_by(token=token, created_by=current_user.id).first_or_404()
    PptInteraction.query.filter_by(session_id=sess.id, is_active=True) \
                        .update({'is_active': False})
    db.session.commit()
    return jsonify({'ok': True})


# ── 公共：学生轮询状态 ───────────────────────────────────
@api.route('/ppt/<token>/state', methods=['GET'])
def get_ppt_state(token):
    sess   = PptSession.query.filter_by(token=token).first_or_404()
    slides = _slides(sess)
    # 助教本地浏览：?index=N 返回指定页（不写库），保留教师停留位置
    browse_idx = request.args.get('index', type=int)
    if browse_idx is not None and slides:
        idx = max(0, min(len(slides) - 1, browse_idx))
    else:
        idx = sess.current_slide
    slide  = None
    if slides and 0 <= idx < len(slides):
        s     = slides[idx]
        slide = {
            'type': s['type'],
            'url':  _slide_url(sess, s["path"]),
            'name': s.get('name', ''),
        }
    ia = PptInteraction.query.filter_by(session_id=sess.id, is_active=True) \
                             .order_by(PptInteraction.created_at.desc()).first()
    def _ia_dict(ia):
        return {
            'id':       ia.id,
            'type':     ia.itype,
            'question': ia.question,
            'options':  json.loads(ia.options_json) if ia.options_json else [],
        }
    return jsonify({
        'current_slide': idx,
        'total_slides':  len(slides),
        'slide':         slide,
        'interact':      _ia_dict(ia) if ia else None,
    })


# ── 公共：互动结果轮询 ───────────────────────────────────
@api.route('/ppt/<token>/results', methods=['GET'])
def get_ppt_results(token):
    sess = PptSession.query.filter_by(token=token).first_or_404()
    ia   = PptInteraction.query.filter_by(session_id=sess.id, is_active=True) \
                               .order_by(PptInteraction.created_at.desc()).first()
    if not ia:
        ia = PptInteraction.query.filter_by(session_id=sess.id) \
                                 .order_by(PptInteraction.created_at.desc()).first()
        if not ia:
            return jsonify({'interact': None, 'results': {}})

    resps = sorted(ia.responses, key=lambda r: r.created_at)

    if ia.itype == 'choice':
        cnts = {'A': 0, 'B': 0, 'C': 0}
        for r in resps:
            if r.answer in cnts:
                cnts[r.answer] += 1
        results = {'counts': cnts, 'total': len(resps)}

    elif ia.itype == 'vote':
        v1 = sum(1 for r in resps if r.answer == '1')
        v2 = sum(1 for r in resps if r.answer == '2')
        reasons = [{'side': r.answer, 'text': r.reason}
                   for r in resps if r.reason]
        results = {'v1': v1, 'v2': v2, 'total': v1 + v2, 'reasons': reasons}

    else:  # short
        answers = [{'text': r.reason or r.answer, 'sid': r.student_id}
                   for r in resps]
        results = {'answers': answers, 'total': len(answers)}

    return jsonify({
        'interact': {
            'id':       ia.id,
            'type':     ia.itype,
            'is_active': ia.is_active,
            'question': ia.question,
            'options':  json.loads(ia.options_json) if ia.options_json else [],
        },
        'results':  results,
    })


# ── 公共：学生提交回答 ───────────────────────────────────
@api.route('/ppt/<token>/respond', methods=['POST'])
def ppt_respond(token):
    sess = PptSession.query.filter_by(token=token).first_or_404()
    ia   = PptInteraction.query.filter_by(session_id=sess.id, is_active=True) \
                               .order_by(PptInteraction.created_at.desc()).first()
    if not ia:
        return jsonify({'error': '当前没有活跃的互动环节'}), 404
    data       = request.get_json(silent=True) or {}
    student_id = str(data.get('student_id', '')).strip()
    answer     = str(data.get('answer', '')).strip()
    reason     = str(data.get('reason', '')).strip()[:300]
    if not student_id:
        return jsonify({'error': '请填写学号'}), 400

    # 学号名单校验
    valid, msg = validate_student_id(student_id)
    if not valid:
        return jsonify({'error': msg}), 400

    if PptResponse.query.filter_by(interaction_id=ia.id, student_id=student_id).first():
        return jsonify({'error': '你已提交，不能重复作答', 'already': True}), 409

    if ia.itype == 'choice':
        if answer not in ('A', 'B', 'C'):
            return jsonify({'error': '请选择 A、B 或 C'}), 400
        resp = PptResponse(interaction_id=ia.id, student_id=student_id, answer=answer)
    elif ia.itype == 'vote':
        if answer not in ('1', '2'):
            return jsonify({'error': '请选择方案1或方案2'}), 400
        resp = PptResponse(interaction_id=ia.id, student_id=student_id,
                           answer=answer, reason=reason or None)
    else:  # short
        if not answer:
            return jsonify({'error': '回答不能为空'}), 400
        resp = PptResponse(interaction_id=ia.id, student_id=student_id,
                           answer='short', reason=answer[:300])

    ip  = (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '').split(',')[0].strip()[:45]
    ua  = (request.headers.get('User-Agent', '') or '')[:120]
    resp.ip_addr = ip or None
    resp.ua_hint = ua or None
    db.session.add(resp)
    db.session.commit()

    # 追加课堂日志
    type_label = {'choice': '选择题', 'vote': '投票', 'short': '简答'}.get(ia.itype, ia.itype)
    log_answer = answer if ia.itype != 'short' else (reason or answer)
    log_reason = f'  留言：{reason}' if reason and ia.itype == 'vote' else ''
    _append_log(token, f'{type_label} | {student_id} | {log_answer}{log_reason}')

    return jsonify({'ok': True})


@api.route('/ppt/<token>/html_interact', methods=['POST'])
def ppt_html_interact(token):
    """学生操作 PPT 中 HTML 动画页时上报。读 sign_sid cookie 取学号（签到时写入），
    按 (学号, 课件, slide_path) 去重记录 HtmlInteraction。未签到（无 cookie）不记录。"""
    student_id = (request.cookies.get('sign_sid') or '').strip()
    if not student_id:
        return jsonify({'ok': False, 'reason': 'no_sign'})   # 未签到，不记录
    sess = PptSession.query.filter_by(token=token).first_or_404()
    data = request.get_json(silent=True) or {}
    slide_path = str(data.get('slide_path', '')).strip()[:200]
    if not slide_path:
        return jsonify({'ok': False, 'error': 'slide_path 不能为空'}), 400
    # 去重：同一学号同一课件同一 HTML 页只记一次
    exists = HtmlInteraction.query.filter_by(
        student_id=student_id, ppt_session_id=sess.id, slide_path=slide_path).first()
    if not exists:
        db.session.add(HtmlInteraction(
            student_id=student_id, ppt_session_id=sess.id, slide_path=slide_path))
        db.session.commit()
    return jsonify({'ok': True, 'recorded': not exists})


# ── 公共：二维码图片 ─────────────────────────────────────
@api.route('/ppt/<token>/qr.png', methods=['GET'])
@login_required
def ppt_qr(token):
    import qrcode
    from qrcode.image.pure import PyPNGImage
    PptSession.query.filter_by(token=token).first_or_404()
    # 使用 url_for 生成外部链接，ProxyFix 会自动加上 /ZZLX 前缀
    from flask import url_for
    url  = url_for('main.student_ppt_page', token=token, _external=True)
    qr   = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                          box_size=10, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img  = qr.make_image(image_factory=PyPNGImage)
    buf  = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ── 教师：查询本次课堂互动（用于结束前复现） ─────────────
@api.route('/ppt/<token>/today_interactions', methods=['GET'])
@login_required
@_teacher_required
def get_today_interactions(token):
    """
    返回本次 PPT session 中今日（或2小时内）的所有互动及回答数据。
    用于"结束"后的互动复现 modal。
    """
    from datetime import timedelta
    from ..models import PptResponse

    sess = PptSession.query.filter_by(token=token).first_or_404()
    # 权限：教师只能看自己的会话；高级测试账号可以看任意会话
    if not current_user.is_adv_test and sess.created_by != current_user.id:
        return jsonify({'error': '无权访问'}), 403

    # 召回该课件全部未屏蔽互动（不再限今日/2小时，跨天复用同一课件也能召回）
    interactions = PptInteraction.query.filter(
        PptInteraction.session_id == sess.id,
        PptInteraction.is_blocked == False          # 屏蔽的互动不进入结束召回复现
    ).order_by(PptInteraction.created_at.asc()).all()

    result = []
    for ia in interactions:
        responses = PptResponse.query.filter_by(interaction_id=ia.id)\
                        .order_by(PptResponse.created_at.asc()).all()
        opts = json.loads(ia.options_json) if ia.options_json else []

        if ia.itype == 'choice':
            counts = {'A': 0, 'B': 0, 'C': 0}
            for r in responses:
                if r.answer in counts:
                    counts[r.answer] += 1
            res_data = {'counts': counts, 'total': len(responses)}
        elif ia.itype == 'vote':
            v1 = sum(1 for r in responses if r.answer == '1')
            v2 = sum(1 for r in responses if r.answer == '2')
            res_data = {'v1': v1, 'v2': v2, 'total': v1 + v2,
                        'reasons': [{'side': r.answer, 'text': r.reason}
                                    for r in responses if r.reason]}
        else:  # short
            res_data = {'answers': [{'text': r.reason or r.answer, 'sid': r.student_id}
                                    for r in responses],
                        'total': len(responses)}

        result.append({
            'id':       ia.id,
            'type':     ia.itype,
            'question': ia.question,
            'options':  opts,
            'results':  res_data,
            'created_at': ia.created_at.strftime('%H:%M'),
        })

    return jsonify({'interactions': result})


def _normalize_text(s):
    """简单文本规范化，用于简答题答案相似度匹配。"""
    s = (s or '').strip()
    # 去除常见虚词和标点
    s = re.sub(r'[，。？！、,.?!…·～—\-\s]', '', s)
    s = re.sub(r'[的了吗呢啊哦嗯哈吧呀么]', '', s)
    return s.lower()


# ── 教师：开始课堂（初始化课堂日志） ──────────────────
@api.route('/ppt/<token>/start_log', methods=['POST'])
@login_required
@_teacher_required
def start_ppt_log(token):
    """演示页加载时调用：创建课堂日志头部（已存在则不覆盖）。"""
    sess = PptSession.query.filter_by(token=token).first_or_404()
    if not current_user.is_adv_test and sess.created_by != current_user.id:
        return jsonify({'error': '无权访问'}), 403
    path = _log_path(token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f'# 课堂日志：{sess.title}\n\n')
                f.write(f'**开始时间：** {datetime.now().strftime("%Y-%m-%d %H:%M")}\n')
        except Exception:
            pass
    return jsonify({'success': True})


# ── 教师：结束课堂（关闭互动 + 发放积分） ──────────────
@api.route('/ppt/<token>/end', methods=['POST'])
@login_required
@_teacher_required
@_adv_test_readonly
def end_ppt_session(token):
    """
    教师点击"结束"按钮后确认时调用。
    POST body (optional):
      {
        "correct_answers": {
          "<ia_id>": "A" | "B" | "C"           # 选择题
          "<ia_id>": "1" | "2"                 # 投票
          "<ia_id>": ["sid1", "sid2", ...]     # 简答：选中的学号 + 相似回答
          "<ia_id>": {"sids": [...], "texts": ["参考答案1", ...]}  # 简答扩展
        }
      }
    积分规则：
      - 参与基础积分 +0.5（每次互动，之前为 +1.0）
      - 活跃度排名加成（前10名，不变）
      - 答对 +1（interact_correct，新增）
    """
    from ..models import PptResponse, PointLog, User

    sess = PptSession.query.filter_by(token=token, created_by=current_user.id).first_or_404()
    now  = datetime.now()
    path = _log_path(token)
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'\n---\n\n**结束时间：** {now.strftime("%Y-%m-%d %H:%M")}\n')
    except Exception:
        pass

    data = request.get_json(silent=True) or {}
    correct_answers = data.get('correct_answers', {})  # {str(ia_id): value}

    # 停用所有活跃互动
    all_interactions = PptInteraction.query.filter_by(session_id=sess.id).all()
    for ia in all_interactions:
        ia.is_active = False

    # ── 积分发放 ──────────────────────────────────────────
    RANK_BONUS = [2.0, 1.5, 1.0, 0.5, 0.5, 0.3, 0.3, 0.3, 0.2, 0.2]
    RANK_REASONS = [
        'interact_rank_1', 'interact_rank_2', 'interact_rank_3',
        'interact_rank_4_5', 'interact_rank_4_5',
        'interact_rank_6_8', 'interact_rank_6_8', 'interact_rank_6_8',
        'interact_rank_9_10', 'interact_rank_9_10',
    ]
    pts_base = 0.5  # 参与基础积分（从 1.0 改为 0.5）

    for ia in all_interactions:
        if ia.is_blocked:
            continue   # 屏蔽的互动：不参与结束积分召回（不发参与分/排名分/答对分）
        responses = PptResponse.query.filter_by(interaction_id=ia.id)\
                        .order_by(PptResponse.created_at.asc()).all()

        # 确定正确答案集合（correct_sids）
        correct_sids = set()
        ia_correct = correct_answers.get(str(ia.id))
        if ia_correct is not None:
            if ia.itype in ('choice', 'vote'):
                # 字符串答案，直接比较 resp.answer
                correct_ans_str = str(ia_correct).upper() if ia.itype == 'choice' else str(ia_correct)
                for r in responses:
                    if r.answer == correct_ans_str:
                        correct_sids.add(r.student_id)
            elif ia.itype == 'short':
                # 支持两种格式：列表 [sid, ...] 或 {"sids": [...], "texts": [...]}
                if isinstance(ia_correct, list):
                    selected_sids = set(str(s) for s in ia_correct)
                    ref_texts = []
                elif isinstance(ia_correct, dict):
                    selected_sids = set(str(s) for s in ia_correct.get('sids', []))
                    ref_texts = [str(t) for t in ia_correct.get('texts', [])]
                else:
                    selected_sids = set()
                    ref_texts = []

                norm_refs = [_normalize_text(t) for t in ref_texts]
                for r in responses:
                    if r.student_id in selected_sids:
                        correct_sids.add(r.student_id)
                    elif norm_refs:
                        resp_norm = _normalize_text(r.reason or r.answer)
                        if resp_norm and any(resp_norm == ref for ref in norm_refs):
                            correct_sids.add(r.student_id)

        participated = set()
        for rank_0, resp in enumerate(responses):
            sid = resp.student_id
            if sid in participated:
                continue
            participated.add(sid)

            user = User.query.filter_by(student_id=sid).first()
            if not user:
                continue

            # 参与基础积分 +0.5
            already = PointLog.query.filter_by(
                user_id=user.id, reason='interact_join',
                ref_session_id=ia.id
            ).first()
            if not already:
                user.reward_points = (user.reward_points or 0) + pts_base
                db.session.add(PointLog(
                    user_id=user.id, points=pts_base,
                    reason='interact_join',
                    ref_session_id=ia.id,
                    memo=f'PPT互动参与 session#{sess.id} ia#{ia.id}'
                ))

            # 活跃度排名奖励（前10名）
            if rank_0 < len(RANK_BONUS):
                bonus = RANK_BONUS[rank_0]
                reason = RANK_REASONS[rank_0]
                already_rank = PointLog.query.filter_by(
                    user_id=user.id, reason=reason,
                    ref_session_id=ia.id
                ).first()
                if not already_rank:
                    user.reward_points = (user.reward_points or 0) + bonus
                    db.session.add(PointLog(
                        user_id=user.id, points=bonus,
                        reason=reason,
                        ref_session_id=ia.id,
                        memo=f'PPT活跃第{rank_0+1}名 ia#{ia.id}'
                    ))

            # 答对积分 +1
            if sid in correct_sids:
                already_correct = PointLog.query.filter_by(
                    user_id=user.id, reason='interact_correct',
                    ref_session_id=ia.id
                ).first()
                if not already_correct:
                    user.reward_points = (user.reward_points or 0) + 1.0
                    db.session.add(PointLog(
                        user_id=user.id, points=1.0,
                        reason='interact_correct',
                        ref_session_id=ia.id,
                        memo=f'PPT互动答对 ia#{ia.id}'
                    ))

    db.session.commit()
    return jsonify({'ok': True})
