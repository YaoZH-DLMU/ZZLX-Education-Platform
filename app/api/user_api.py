"""
用户相关 API
POST /api/user/avatar          - 上传/更换头像（multipart/form-data, field=avatar）
POST /api/user/profile         - 更新个人信息（bio, profile_bg_color）
GET  /api/user/<id>/stats      - 获取用户统计数据（供悬浮名片使用）
GET  /api/user/<id>/notifications - 获取指定用户通知（仅本人或管理员）
"""
import json
import os
import re
import threading
import sys
from collections import Counter
from io import BytesIO
from flask import jsonify, request, current_app
from flask_login import current_user, login_required
from PIL import Image
from werkzeug.utils import secure_filename
from . import api
from ..models import User, Video, VideoRating, VideoFavorite, VideoComment, \
                     Notification, StarConfig, compute_stars, compute_stats, db, VideoAIScore, \
                     VideoWorkConfig, UserVideoFinalScore, AIScoringJob, PointLog
from datetime import datetime, timedelta

AVATAR_MAX_SIZE   = (200, 200)   # px
AVATAR_MAX_BYTES  = 100 * 1024   # 100 KB
AVATAR_ALLOWED    = {'png', 'jpg', 'jpeg', 'webp'}


def _avatar_folder():
    from app.utils.course_paths import upload_folder as _uf
    folder = os.path.join(_uf(), 'avatars')
    os.makedirs(folder, exist_ok=True)
    return folder


@api.route('/user/avatar', methods=['POST'])
@login_required
def upload_avatar():
    """
    接受头像图片，自动裁剪为 200×200，限制 100KB 以内后保存。
    返回新的头像 URL。
    """
    if 'avatar' not in request.files:
        return jsonify({'error': '未上传文件'}), 400

    f = request.files['avatar']
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in AVATAR_ALLOWED:
        return jsonify({'error': '仅支持 PNG / JPG / WEBP 格式'}), 400

    try:
        img = Image.open(f.stream).convert('RGBA')
    except Exception:
        return jsonify({'error': '图片读取失败'}), 400

    # 中心裁剪为正方形再缩放到 200×200
    w, h = img.size
    side  = min(w, h)
    left  = (w - side) // 2
    top   = (h - side) // 2
    img   = img.crop((left, top, left + side, top + side))
    img   = img.resize(AVATAR_MAX_SIZE, Image.LANCZOS)

    # 转为 PNG 并检查文件大小
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    if buf.tell() > AVATAR_MAX_BYTES:
        # 降质压缩
        buf = BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=75, optimize=True)
        if buf.tell() > AVATAR_MAX_BYTES:
            return jsonify({'error': '图片压缩后仍超过 100KB，请选择更小的图片'}), 400
        save_ext = 'jpg'
    else:
        save_ext = 'png'

    filename = f'avatar_{current_user.id}.{save_ext}'
    save_path = os.path.join(_avatar_folder(), filename)
    buf.seek(0)
    with open(save_path, 'wb') as out:
        out.write(buf.read())

    # 更新数据库
    current_user.avatar = filename
    db.session.commit()

    return jsonify({
        'success':    True,
        'avatar_url': current_user.avatar_url,
    })


@api.route('/user/profile', methods=['POST'])
@login_required
def update_profile():
    data = request.get_json(silent=True) or {}

    # 允许更新的白名单字段
    allowed_bg_colors = {
        # 冷色系（1-3星解锁）
        '#e8f4f8', '#dce8f5', '#e0eaf8', '#e4f0fb',
        '#e8f3ec', '#e0f0ea', '#eaf4e8', '#e6f0dc',
        # 中性色（4星解锁）
        '#f5f5f0', '#f0f0e8', '#ede8e0', '#e8e4dc',
        # 暖色系（5星解锁）
        '#fdf5e8', '#fceee0', '#fde8e0', '#fce8ec',
        # 默认
        '#ffffff',
    }

    if 'bio' in data:
        bio = str(data['bio'])[:200]
        current_user.bio = bio

    if 'profile_bg_color' in data:
        color = str(data['profile_bg_color']).lower()
        if color in allowed_bg_colors:
            current_user.profile_bg_color = color
        else:
            return jsonify({'error': '不支持该背景色'}), 400

    db.session.commit()
    return jsonify({'success': True})


@api.route('/user/password', methods=['POST'])
@login_required
def change_password():
    """修改密码：需提供旧密码验证身份"""
    data   = request.get_json(silent=True) or {}
    old_pw = data.get('old_password', '')
    new_pw = data.get('new_password', '')

    if not old_pw or not new_pw:
        return jsonify({'error': '旧密码和新密码不能为空'}), 400
    if not current_user.check_password(old_pw):
        return jsonify({'error': '旧密码错误，请重新输入'}), 400
    if len(new_pw) < 6:
        return jsonify({'error': '新密码至少需要 6 个字符'}), 400
    if new_pw == current_user.student_id:
        return jsonify({'error': '新密码不能与学号相同'}), 400

    current_user.set_password(new_pw)
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────────
# 第4阶段：教师星级阈値配置
# ─────────────────────────────────────────────────

@api.route('/teacher/star_config', methods=['GET'])
def get_star_config():
    """获取当前第4、5星阈值及 AI 权重配置"""
    cfg = StarConfig.get_config()
    return jsonify({
        's4_min_views':          cfg.s4_min_views,
        's4_min_favorites':      cfg.s4_min_favorites,
        's5_min_videos':         cfg.s5_min_videos,
        's5_min_avg_rating':     cfg.s5_min_avg_rating,
        's5_min_video_comments': cfg.s5_min_video_comments,
        'teacher_weight':        cfg.teacher_weight,
        'ai_weight':             cfg.ai_weight,
    })


@api.route('/teacher/star_config', methods=['POST'])
@login_required
def update_star_config():
    """教师更新第4、5星阈值及 AI 权重"""
    if not current_user.is_teacher:
        return jsonify({'error': '无权限，仅教师可修改'}), 403

    data = request.get_json(silent=True) or {}
    cfg  = StarConfig.get_config()

    INT_FIELDS   = ['s4_min_views', 's4_min_favorites', 's5_min_videos',
                    's5_min_video_comments', 'teacher_weight', 'ai_weight']
    FLOAT_FIELDS = ['s5_min_avg_rating']

    for f in INT_FIELDS:
        if f in data:
            val = int(data[f])
            if val < 0:
                return jsonify({'error': f'{f} 不能为负数'}), 400
            setattr(cfg, f, val)

    for f in FLOAT_FIELDS:
        if f in data:
            val = float(data[f])
            if not (0 <= val <= 10):
                return jsonify({'error': f'{f} 应在 0-10 之间'}), 400
            setattr(cfg, f, val)

    cfg.updated_by = current_user.id
    db.session.commit()
    return jsonify({'success': True, 'message': '配置已保存'})


# ─────────────────────────────────────────────────
# 第8阶段：视频作业配置 & 最终成绩重算
# ─────────────────────────────────────────────────

_CN_NUMS = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9,
}


def _cn_to_int(s):
    """将中文数字字符串（如 '十三'）转换为整数，支持 1-99。"""
    if s == '十':
        return 10
    if s.startswith('十'):
        return 10 + _CN_NUMS.get(s[1:], 0)
    if '十' in s:
        parts = s.split('十', 1)
        tens = _CN_NUMS.get(parts[0], 0) * 10
        ones = _CN_NUMS.get(parts[1], 0) if parts[1] else 0
        return tens + ones
    return _CN_NUMS.get(s, 0)


def _parse_chapter_num(label):
    """从 '第X章 …' 格式的标签中提取章节整数编号，失败返回 None。"""
    m = re.match(r'第(.+?)章', label)
    if m:
        num = _cn_to_int(m.group(1))
        return num if num > 0 else None
    return None


def _recalc_video_final_scores():
    """
    根据 VideoWorkConfig 重新计算所有学生的视频作业最终成绩。
    算法：对每个学生，在每个视频作业对应的章节范围内取最高综合评分，
         最终成绩 = (各视频作业最高分之和) / N。
    返回：已更新的学生人数。
    """
    from datetime import datetime as _dt
    from sqlalchemy import func as sqlfunc

    wk_cfg   = VideoWorkConfig.get_config()
    mappings = wk_cfg.get_mappings()
    n        = wk_cfg.n

    if not mappings or n <= 0:
        return 0

    star_cfg  = StarConfig.get_config()
    w_teacher = star_cfg.teacher_weight if star_cfg.teacher_weight is not None else 3
    w_ai      = star_cfg.ai_weight      if star_cfg.ai_weight      is not None else 2

    # ── 批量预取所有 homework 视频 ──
    hw_videos = Video.query.filter_by(type='homework').all()
    if not hw_videos:
        # 无视频时将所有学生成绩归零
        students = User.query.filter_by(is_teacher=False).all()
        for student in students:
            rec = UserVideoFinalScore.query.filter_by(user_id=student.id).first()
            if rec:
                rec.score = 0.0
                rec.detail_json = '{}'
            else:
                db.session.add(UserVideoFinalScore(user_id=student.id, score=0.0))
        db.session.commit()
        return len(students)

    vid_ids = [v.id for v in hw_videos]

    # ── 教师 id 列表 ──
    teacher_ids = [u.id for u in User.query.filter_by(is_teacher=True).all()]

    # ── 学生评分：video_id -> (sum, count) ──
    stu_q = db.session.query(
        VideoRating.video_id,
        sqlfunc.sum(VideoRating.value).label('s'),
        sqlfunc.count(VideoRating.id).label('c'),
    ).filter(VideoRating.video_id.in_(vid_ids))
    if teacher_ids:
        stu_q = stu_q.filter(VideoRating.user_id.notin_(teacher_ids))
    stu_map = {r.video_id: (float(r.s or 0), int(r.c or 0))
               for r in stu_q.group_by(VideoRating.video_id)}

    # ── 教师评分：video_id -> avg ──
    if teacher_ids:
        tea_rows = db.session.query(
            VideoRating.video_id,
            sqlfunc.avg(VideoRating.value).label('a'),
        ).filter(
            VideoRating.video_id.in_(vid_ids),
            VideoRating.user_id.in_(teacher_ids),
        ).group_by(VideoRating.video_id).all()
        tea_map = {r.video_id: float(r.a) for r in tea_rows}
    else:
        tea_map = {}

    # ── AI 评分：video_id -> score ──
    ai_rows = VideoAIScore.query.filter(VideoAIScore.video_id.in_(vid_ids)).all()
    ai_map  = {a.video_id: a.score for a in ai_rows}

    # ── 计算每个视频的综合评分 ──
    def _composite(vid_id):
        s_sum, s_cnt = stu_map.get(vid_id, (0.0, 0))
        t_avg        = tea_map.get(vid_id)
        a_score      = ai_map.get(vid_id)
        total_score  = s_sum
        total_weight = float(s_cnt)
        if t_avg is not None:
            total_score  += t_avg * w_teacher
            total_weight += w_teacher
        if a_score is not None:
            total_score  += a_score * w_ai
            total_weight += w_ai
        return round(total_score / total_weight, 2) if total_weight > 0 else 0.0

    # ── 构建 (user_id, chapter) -> [score, ...] 索引 ──
    uc_map = {}
    for v in hw_videos:
        if v.chapter is None:
            continue
        score = _composite(v.id)
        key   = (v.user_id, v.chapter)
        uc_map.setdefault(key, []).append(score)

    # ── 计算每个学生的最终成绩 ──
    students = User.query.filter_by(is_teacher=False).all()
    count    = 0
    for student in students:
        details = {}
        total   = 0.0
        for mapping in mappings[:n]:
            vi       = mapping.get('video_index', 0)
            chapters = mapping.get('chapters', [])
            max_s    = 0.0
            for ch in chapters:
                for sc in uc_map.get((student.id, int(ch)), []):
                    if sc > max_s:
                        max_s = sc
            details[str(vi)] = round(max_s, 2)
            total += max_s
        final = round(total / n, 2) if n > 0 else 0.0

        rec = UserVideoFinalScore.query.filter_by(user_id=student.id).first()
        if rec:
            rec.score       = final
            rec.detail_json = json.dumps(details)
            rec.updated_at  = _dt.utcnow()
        else:
            db.session.add(UserVideoFinalScore(
                user_id=student.id,
                score=final,
                detail_json=json.dumps(details),
            ))
        count += 1

    db.session.commit()
    return count


@api.route('/teacher/qg_chapters', methods=['GET'])
def get_qg_chapters():
    """返回 QG.json 中的 level1 章节列表（阿拉伯数字编号 + 显示标签）。"""
    from app.utils.course_paths import qg_path as _qg_path_func
    qg_path = _qg_path_func()
    try:
        with open(qg_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({'error': f'读取 QG.json 失败：{e}'}), 500

    chapters = []
    for node in data.get('children', []):
        label = node.get('label', '')
        num   = _parse_chapter_num(label)
        if num:
            chapters.append({'num': num, 'label': f'第{num}章'})

    chapters.sort(key=lambda x: x['num'])
    return jsonify({'chapters': chapters})


@api.route('/teacher/video_work_config', methods=['GET'])
def get_video_work_config():
    """返回当前视频作业配置（N 及各视频作业章节映射）以及所有相关权重。"""
    cfg = VideoWorkConfig.get_config()
    sc  = StarConfig.get_config()
    return jsonify({
        'n':        cfg.n,
        'mappings': cfg.get_mappings(),
        'meaningful_threshold': sc.meaningful_threshold,
        'meaningful_weight':    sc.meaningful_weight,
        'point_exchange_rate':  sc.point_exchange_rate,
    })


@api.route('/teacher/video_work_config', methods=['POST'])
@login_required
def update_video_work_config():
    """
    教师保存视频作业配置，并立即重算所有学生成绩。
    可选字段 teacher_weight / ai_weight：若传入则同步更新 StarConfig。
    """
    if not current_user.is_teacher:
        return jsonify({'error': '无权限，仅教师可修改'}), 403

    data = request.get_json(silent=True) or {}

    # ── 验证 n ──
    try:
        n = int(data.get('n', 3))
        if not (1 <= n <= 5):
            return jsonify({'error': 'n 应在 1-5 之间'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'n 格式错误'}), 400

    # ── 验证 mappings ──
    mappings = data.get('mappings', [])
    if not isinstance(mappings, list) or len(mappings) == 0:
        return jsonify({'error': 'mappings 不能为空'}), 400
    for m in mappings:
        if not isinstance(m.get('chapters'), list) or len(m['chapters']) == 0:
            return jsonify({'error': f'视频作业 {m.get("video_index")} 未选择任何章节'}), 400

    # ── 保存配置 ──
    cfg = VideoWorkConfig.get_config()
    cfg.n          = n
    cfg.set_mappings(mappings)
    cfg.updated_by = current_user.id

    # ── 可选：同步更新 AI 权重 ──
    if 'teacher_weight' in data or 'ai_weight' in data:
        star_cfg = StarConfig.get_config()
        if 'teacher_weight' in data:
            star_cfg.teacher_weight = max(0, int(data['teacher_weight']))
        if 'ai_weight' in data:
            star_cfg.ai_weight = max(0, int(data['ai_weight']))
        star_cfg.updated_by = current_user.id

    # ── 有意义回复配置 ──
    if 'meaningful_threshold' in data:
        star_cfg.meaningful_threshold = max(0, int(data['meaningful_threshold']))
    if 'meaningful_weight' in data:
        star_cfg.meaningful_weight = max(0.0, float(data['meaningful_weight']))
    if 'point_exchange_rate' in data:
        star_cfg.point_exchange_rate = max(1, int(data['point_exchange_rate']))

    db.session.commit()

    # ── 重算成绩 ──
    updated = _recalc_video_final_scores()

    return jsonify({'success': True, 'updated': updated})


@api.route('/user/<int:user_id>/stats')
def get_user_stats(user_id):
    """
    返回用户统计数据，供视频卡片悬浮名片使用。
    公开接口，无需登录。
    """
    user = User.query.get_or_404(user_id)

    video_count    = Video.query.filter_by(user_id=user_id).count()
    comment_count  = VideoComment.query.filter_by(user_id=user_id).count()
    favorite_count = VideoFavorite.query.filter_by(user_id=user_id).count()

    # 平均评分（自己发布的视频被他人评分的均值）
    from sqlalchemy import func
    avg_rating_raw = db.session.query(func.avg(VideoRating.value))\
        .join(Video, VideoRating.video_id == Video.id)\
        .filter(Video.user_id == user_id)\
        .scalar()
    avg_rating = round(float(avg_rating_raw), 1) if avg_rating_raw else 0.0

    # 累计被观看数
    total_views_raw = db.session.query(func.sum(Video.views))\
        .filter(Video.user_id == user_id).scalar()
    total_views = int(total_views_raw) if total_views_raw else 0

    stats = compute_stats(user)
    stars = compute_stars(user)

    return jsonify({
        'id':            user.id,
        'username':      user.username,
        'avatar_url':    user.avatar_url,
        'bio':           user.bio or '',
        'is_teacher':    user.is_teacher,
        'video_count':   video_count,
        'comment_count': comment_count,
        'favorite_count':favorite_count,
        'avg_rating':    avg_rating,
        'total_views':   total_views,
        'created_at':    user.created_at.strftime('%Y-%m') if user.created_at else '',
        # 第4阶段新增
        'stars':         stars,
        'attack':        stats['attack'],
        'defense':       stats['defense'],
        'magic':         stats['magic'],
        'profile_bg_color': user.profile_bg_color or '#ffffff',
    })


# ─────────────────────────────────────────────────
# 课程管理 — 数据管理接口
# ─────────────────────────────────────────────────

def _teacher_required_api(f):
    """API 用的教师权限装饰器（高级测试账号 is_adv_test 也可只读访问）"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or \
                not (current_user.is_teacher or current_user.is_adv_test):
            return jsonify({'error': '仅教师账号可操作'}), 403
        return f(*args, **kwargs)
    return decorated


def _adv_test_readonly_api(f):
    """阻止高级测试账号执行写入/下载操作"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.is_authenticated and current_user.is_adv_test:
            return jsonify({'error': '观察账号不可执行此操作'}), 403
        return f(*args, **kwargs)
    return decorated


@api.route('/teacher/data/stats')
@login_required
@_teacher_required_api
def data_stats():
    """当前数据库各表计数快照"""
    from app.models import VideoComment, VideoRating, VideoFavorite
    students = User.query.filter(
        User.is_teacher == False,
        db.func.length(User.student_id) >= 10
    ).count()
    videos   = Video.query.count()
    homework = Video.query.filter_by(type='homework').count()
    flipped  = Video.query.filter_by(type='flipped').count()
    comments = VideoComment.query.count()
    ratings  = VideoRating.query.count()
    return jsonify({
        'students': students,
        'videos':   videos,
        'homework': homework,
        'flipped':  flipped,
        'comments': comments,
        'ratings':  ratings,
    })


@api.route('/teacher/data/export_csv')
@login_required
@_teacher_required_api
@_adv_test_readonly_api
def data_export_csv():
    """
    将主要数据表导出为单 ZIP 文件（内含多个 CSV），供教师本地备份。
    保留所有用户、视频、评论、评分、收藏、签到、PPT 互动记录。
    """
    import csv, io, zipfile
    from app.models import VideoComment, VideoRating, VideoFavorite, \
        SignSession, SignResponse, PptSession, PptInteraction, PptResponse
    from flask import Response

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:

        def csv_bytes(headers, rows):
            s = io.StringIO()
            w = csv.writer(s)
            w.writerow(headers)
            w.writerows(rows)
            return s.getvalue().encode('utf-8-sig')

        # 用户
        zf.writestr('users.csv', csv_bytes(
            ['id', 'username', 'student_id', 'is_teacher', 'created_at'],
            [(u.id, u.username, u.student_id, u.is_teacher,
              u.created_at.strftime('%Y-%m-%d %H:%M') if u.created_at else '')
             for u in User.query.all()]
        ))
        # 视频
        zf.writestr('videos.csv', csv_bytes(
            ['id', 'title', 'type', 'subject', 'chapter', 'problem_no',
             'group_no', 'class_no', 'user_id', 'views', 'avg_rating', 'created_at'],
            [(v.id, v.title, v.type, v.subject, v.chapter, v.problem_no,
              v.group_no, v.class_no, v.user_id, v.views,
              v.average_rating,
              v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else '')
             for v in Video.query.all()]
        ))
        # 评论
        zf.writestr('comments.csv', csv_bytes(
            ['id', 'video_id', 'user_id', 'content', 'is_meaningful', 'created_at'],
            [(c.id, c.video_id, c.user_id, c.content, c.is_meaningful,
              c.created_at.strftime('%Y-%m-%d %H:%M'))
             for c in VideoComment.query.all()]
        ))
        # 评分
        zf.writestr('ratings.csv', csv_bytes(
            ['id', 'video_id', 'user_id', 'value', 'created_at'],
            [(r.id, r.video_id, r.user_id, r.value,
              r.created_at.strftime('%Y-%m-%d %H:%M'))
             for r in VideoRating.query.all()]
        ))
        # 签到
        zf.writestr('sign_sessions.csv', csv_bytes(
            ['id', 'token', 'question', 'created_by', 'created_at', 'is_active'],
            [(s.id, s.token, s.question, s.created_by,
              s.created_at.strftime('%Y-%m-%d %H:%M'), s.is_active)
             for s in SignSession.query.all()]
        ))
        zf.writestr('sign_responses.csv', csv_bytes(
            ['id', 'session_id', 'student_id', 'answer', 'ip_addr', 'created_at'],
            [(r.id, r.session_id, r.student_id, r.answer, r.ip_addr or '',
              r.created_at.strftime('%Y-%m-%d %H:%M'))
             for r in SignResponse.query.all()]
        ))
        # PPT 互动
        zf.writestr('ppt_responses.csv', csv_bytes(
            ['id', 'interaction_id', 'student_id', 'answer', 'reason', 'ip_addr', 'created_at'],
            [(r.id, r.interaction_id, r.student_id, r.answer or '', r.reason or '',
              r.ip_addr or '',
              r.created_at.strftime('%Y-%m-%d %H:%M'))
             for r in PptResponse.query.all()]
        ))

    buf.seek(0)
    from datetime import datetime
    fname = 'zzlx_data_' + datetime.now().strftime('%Y%m%d_%H%M') + '.zip'
    return Response(
        buf.read(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'}
    )


@api.route('/teacher/data/archive_preview')
@login_required
@_teacher_required_api
def data_archive_preview():
    """
    预览「精选归档」将删除的视频列表。
    规则：按(type, subject, chapter, problem_no) / (type, subject, class_no) 分组，
    每组保留平均评分最高的1个，其余标记为待删除。
    """
    from sqlalchemy import func as sqlfunc

    def _group_key(v):
        if v.type == 'homework':
            return ('homework', v.subject or '', v.chapter or 0, v.problem_no or 0)
        else:  # flipped
            return ('flipped', v.subject or '', v.class_no or 0, 0)

    videos = Video.query.all()
    groups = {}
    for v in videos:
        k = _group_key(v)
        groups.setdefault(k, []).append(v)

    to_delete = []
    for k, vlist in groups.items():
        if len(vlist) <= 1:
            continue
        # 按平均评分降序，评分相同则 id 小的优先（上传早的）
        vlist_rated = sorted(vlist, key=lambda v: (-v.average_rating, v.id))
        for v in vlist_rated[1:]:   # 跳过第一个（最高分），其余列为删除
            to_delete.append({
                'id':         v.id,
                'type':       v.type,
                'key':        str(k),
                'title':      v.title,
                'author':     v.author.username if v.author else '?',
                'avg_rating': v.average_rating,
            })

    return jsonify({'to_delete': to_delete, 'count': len(to_delete)})


@api.route('/teacher/data/archive_run', methods=['POST'])
@login_required
@_teacher_required_api
@_adv_test_readonly_api
def data_archive_run():
    """
    执行精选归档：
    - 每个题目/答辩场次只保留平均评分最高的视频
    - 删除低分重复视频的文件（video + thumbnail）及数据库记录
    - 学生账号、评论、评分、收藏绑定在保留视频上的不受影响
    """
    import os
    from flask import current_app
    from app.models import VideoComment, VideoRating, VideoFavorite

    def _group_key(v):
        if v.type == 'homework':
            return ('homework', v.subject or '', v.chapter or 0, v.problem_no or 0)
        else:
            return ('flipped', v.subject or '', v.class_no or 0, 0)

    videos = Video.query.all()
    groups = {}
    for v in videos:
        k = _group_key(v)
        groups.setdefault(k, []).append(v)

    deleted = 0
    kept    = 0
    log_lines = []
    upload_folder = current_app.config.get('UPLOAD_FOLDER', '')

    for k, vlist in groups.items():
        if len(vlist) <= 1:
            kept += len(vlist)
            continue
        vlist_rated = sorted(vlist, key=lambda v: (-v.average_rating, v.id))
        kept += 1
        log_lines.append(f'[保留] {vlist_rated[0].title}  评分:{vlist_rated[0].average_rating}')
        for v in vlist_rated[1:]:
            log_lines.append(f'[删除] {v.title}  评分:{v.average_rating}  id={v.id}')
            # 删除物理文件
            try:
                vid_rel = v.url.replace('/static/', '', 1) if v.url else ''
                vid_path = os.path.join(upload_folder, '..', 'static', vid_rel) if vid_rel else ''
                if vid_path and os.path.isfile(vid_path):
                    os.remove(vid_path)
            except Exception as e:
                log_lines.append(f'  ↳ 文件删除失败: {e}')
            try:
                if v.thumbnail:
                    th_rel = v.thumbnail.replace('/static/', '', 1)
                    th_path = os.path.join(upload_folder, '..', 'static', th_rel)
                    if os.path.isfile(th_path):
                        os.remove(th_path)
            except Exception:
                pass
            db.session.delete(v)
            deleted += 1

    db.session.commit()
    return jsonify({
        'deleted':  deleted,
        'kept':     kept,
        'log':      '\n'.join(log_lines),
    })


@api.route('/teacher/data/export_json')
@login_required
@_teacher_required_api
@_adv_test_readonly_api
def data_export_json():
    """
    按 type 参数导出结构化 JSON（供数据分析标签页读入）。
    type: interactions | sign | scores
    """
    import json as _json
    from flask import Response
    from app.models import PptInteraction, PptResponse, SignSession, SignResponse
    from sqlalchemy import func as sqlfunc
    from datetime import datetime

    dtype = request.args.get('type', 'scores')
    fname_map = {
        'interactions':        'ppt_interactions',
        'sign':                'sign_records',
        'scores':              'student_scores',
        'video_final_scores':  'video_final_scores',
        'meaningful_replies':  'meaningful_replies',
    }

    if dtype == 'interactions':
        rows = []
        for ia in PptInteraction.query.filter_by(is_blocked=False).all():
            for r in ia.responses:
                rows.append({
                    'interaction_id': ia.id,
                    'question':       ia.question or '',
                    'itype':          ia.itype,
                    'student_id':     r.student_id,
                    'answer':         r.answer or '',
                    'reason':         r.reason or '',
                    'created_at':     r.created_at.strftime('%Y-%m-%d %H:%M'),
                })
        data = rows

    elif dtype == 'sign':
        rows = []
        for sess in SignSession.query.all():
            for r in sess.responses:
                rows.append({
                    'session_id': sess.id,
                    'question':   sess.question,
                    'student_id': r.student_id,
                    'answer':     r.answer,
                    'created_at': r.created_at.strftime('%Y-%m-%d %H:%M'),
                })
        data = rows

    elif dtype == 'video_final_scores':
        from app.models import UserVideoFinalScore
        rows = []
        score_map = {r.user_id: r for r in UserVideoFinalScore.query.all()}
        for u in User.query.filter(
            User.is_teacher == False,
            db.func.length(User.student_id) >= 10
        ).order_by(User.student_id.asc()).all():
            rec    = score_map.get(u.id)
            detail = json.loads(rec.detail_json) if rec else {}
            row = {
                'student_id':  u.student_id,
                'username':    u.username,
                'final_score': round(rec.score, 2) if rec else 0.0,
            }
            row.update({f'hw{k}': v for k, v in detail.items()})
            rows.append(row)
        data = rows

    elif dtype == 'meaningful_replies':
        rows = []
        for u in User.query.filter(
            User.is_teacher == False,
            db.func.length(User.student_id) >= 10
        ).order_by(User.student_id.asc()).all():
            rows.append({
                'student_id':             u.student_id,
                'username':               u.username,
                'meaningful_replies_count': u.meaningful_replies_count or 0,
            })
        data = rows

    else:  # scores → 学生积分快照
        rows = []
        for u in User.query.filter(
            User.is_teacher == False,
            db.func.length(User.student_id) >= 10
        ).order_by(User.reward_points.desc(), User.student_id.asc()).all():
            rows.append({
                'student_id':    u.student_id,
                'username':      u.username,
                'reward_points': float(u.reward_points or 0),
                'stars':         compute_stars(u),
                'created_at':    u.created_at.strftime('%Y-%m-%d') if u.created_at else '',
            })
        data = rows

    out = _json.dumps(data, ensure_ascii=False, indent=2)
    fname = fname_map.get(dtype, 'export') + '_' + datetime.now().strftime('%Y%m%d') + '.json'
    return Response(out.encode('utf-8'),
                    mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


# ── 分组柱状图：积分/视频成绩分布对比 ─────────────────────
@api.route('/teacher/data/chart/<chart_type>')
@login_required
@_teacher_required_api
def get_chart_data(chart_type):
    """返回分组柱状图数据。chart_type: points | video_scores。?period=week|month&offset=0"""
    period = request.args.get('period', 'week')
    offset = max(0, request.args.get('offset', 0, type=int))
    now = datetime.utcnow()

    # 计算 3 个时间段的截止点（offset=0: 本周/上周/2周前；offset=1: 上周/2周前/3周前）
    cutoffs, period_labels = [], []
    if period == 'month':
        for i in range(offset, offset + 3):
            y = now.year if now.month - i > 0 else now.year - 1
            m = (now.month - i - 1) % 12 + 1
            if m == 12:
                end = datetime(y + 1, 1, 1)
            else:
                end = datetime(y, m + 1, 1)
            end = min(end, now)
            cutoffs.append(end)
            period_labels.append(f'{m}月')
    else:
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(offset, offset + 3):
            end = min(monday - timedelta(weeks=i), now)
            cutoffs.append(end)
            w = monday - timedelta(weeks=i)
            period_labels.append(f'{w.month}/{w.day}')

    # 倒序：最远的在前（左），最近的在后（右）
    cutoffs.reverse()
    period_labels.reverse()

    series_colors = ['#b0c4de', '#6a9fd8', '#2c5f9e']
    series_names = ['2期前', '1期前', '本期']
    if offset > 0:
        series_names = [f'{offset+2}期前', f'{offset+1}期前', f'{offset}期前']

    if chart_type == 'points':
        # 积分分布：各学生累计积分分区间（0-100范围）
        bins = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
                (50, 60), (60, 70), (70, 80), (80, 90), (90, 9999)]
        bin_labels = ['0-10', '10-20', '20-30', '30-40', '40-50',
                      '50-60', '60-70', '70-80', '80-90', '90+']
        title = '积分分布对比'
        series = []
        for ci, cutoff in enumerate(cutoffs):
            counts = [0] * len(bins)
            rows = db.session.query(
                PointLog.user_id,
                db.func.sum(PointLog.points).label('total')
            ).filter(PointLog.created_at < cutoff).group_by(PointLog.user_id).all()
            valid_uids = set(u.id for u in User.query.filter(
                User.is_teacher == False, db.func.length(User.student_id) >= 8).all())
            for uid, total in rows:
                if uid not in valid_uids:
                    continue
                t = float(total or 0)
                for bi, (lo, hi) in enumerate(bins):
                    if lo <= t < hi:
                        counts[bi] += 1
                        break
            series.append({'name': series_names[ci], 'color': series_colors[ci], 'values': counts})
        return jsonify({'title': title, 'bins': bin_labels, 'periods': period_labels, 'series': series, 'period': period, 'offset': offset})

    elif chart_type == 'video_scores':
        # 视频成绩分布：优先 UserVideoFinalScore，无数据则用 VideoAIScore.score
        from app.models import UserVideoFinalScore, VideoAIScore
        has_final = UserVideoFinalScore.query.filter(UserVideoFinalScore.score > 0).count() > 0
        bins = [(0, 6), (6, 7), (7, 7.5), (7.5, 8), (8, 8.5), (8.5, 9), (9, 10.1)]
        bin_labels = ['<6', '6-7', '7-7.5', '7.5-8', '8-8.5', '8.5-9', '9+']
        title = '视频作业成绩分布对比'
        series = []
        for ci, cutoff in enumerate(cutoffs):
            counts = [0] * len(bins)
            if has_final:
                scores = db.session.query(UserVideoFinalScore.score).filter(
                    UserVideoFinalScore.updated_at < cutoff,
                    UserVideoFinalScore.score > 0
                ).all()
            else:
                scores = db.session.query(VideoAIScore.score).filter(
                    VideoAIScore.created_at < cutoff
                ).all()
            for (s,) in scores:
                s = float(s or 0)
                for bi, (lo, hi) in enumerate(bins):
                    if lo <= s < hi:
                        counts[bi] += 1
                        break
            series.append({'name': series_names[ci], 'color': series_colors[ci], 'values': counts})
        source = '最终加权成绩' if has_final else 'AI评分（无最终成绩数据）'
        return jsonify({'title': title, 'bins': bin_labels, 'periods': period_labels, 'series': series, 'period': period, 'offset': offset, 'source': source})

    else:
        return jsonify({'error': '未知图表类型'}), 400


@api.route('/teacher/analysis/summary')
@login_required
@_teacher_required_api
def analysis_summary():
    """【预留接口】数据分析汇总，供"数据分析"标签页调用，目前返回空数据框架。"""
    return jsonify({
        '_note': '数据分析接口预留，待后续开发',
        'charts': [],
    })


@api.route('/teacher/analysis/report')
@login_required
@_teacher_required_api
def analysis_report():
    score_rows = db.session.query(
        VideoAIScore.score,
        User.username,
        Video.title,
        Video.chapter,
        Video.problem_no,
        VideoAIScore.reason,
    ).join(Video, Video.id == VideoAIScore.video_id) \
     .join(User, User.id == Video.user_id) \
     .order_by(VideoAIScore.score.desc(), User.username.asc(), Video.id.asc()) \
     .all()

    score_values = [float(row.score) for row in score_rows]
    score_dist = Counter(score_values)

    chapter_rows = db.session.query(
        Video.chapter,
        db.func.count(VideoAIScore.id),
        db.func.avg(VideoAIScore.score),
        db.func.min(VideoAIScore.score),
        db.func.max(VideoAIScore.score),
    ).join(Video, Video.id == VideoAIScore.video_id) \
     .group_by(Video.chapter) \
     .order_by(Video.chapter.asc()) \
     .all()

    # 统计时排除测试账号（学号长度 < 10 的均为测试/管理账号）
    users = User.query.filter(
        User.is_teacher == False,
        db.func.length(User.student_id) >= 10
    ).order_by(User.reward_points.desc(), User.username.asc()).all()
    point_values = [float(user.reward_points or 0) for user in users]
    if point_values:
        bucket_size = 10
        max_bucket = int(max(point_values) // bucket_size) * bucket_size
        buckets = []
        current = 0
        while current <= max_bucket:
            upper = current + bucket_size
            buckets.append({
                'label': f'{current}-{upper}',
                'count': sum(1 for value in point_values if current <= value < upper),
            })
            current = upper
    else:
        buckets = []

    return jsonify({
        'score_stats': {
            'count': len(score_values),
            'min': min(score_values) if score_values else None,
            'max': max(score_values) if score_values else None,
            'avg': round(sum(score_values) / len(score_values), 4) if score_values else None,
            'distribution': [
                {'score': score, 'count': score_dist[score]}
                for score in sorted(score_dist.keys())
            ],
            'top_scores': [
                {
                    'score': float(row.score),
                    'username': row.username,
                    'title': row.title,
                    'chapter': row.chapter,
                    'problem_no': row.problem_no,
                    'reason': row.reason or '',
                }
                for row in score_rows[:12]
            ],
        },
        'point_stats': {
            'count': len(point_values),
            'min': min(point_values) if point_values else None,
            'max': max(point_values) if point_values else None,
            'avg': round(sum(point_values) / len(point_values), 4) if point_values else None,
            'student_count': len(users),
            'buckets': buckets,
            'top_users': [
                {
                    'username': user.username,
                    'student_id': user.student_id,
                    'is_teacher': 1 if user.is_teacher else 0,
                    'reward_points': float(user.reward_points or 0),
                }
                for user in users[:20]
            ],
        },
        'chapter_stats': [
            {
                'chapter': row[0],
                'count': int(row[1]),
                'avg': round(float(row[2]), 4) if row[2] is not None else None,
                'min': round(float(row[3]), 1) if row[3] is not None else None,
                'max': round(float(row[4]), 1) if row[4] is not None else None,
            }
            for row in chapter_rows if row[0] is not None
        ],
        'hw_stats': _build_hw_stats(),
    })


def _build_hw_stats():
    """构建视频作业最终成绩分布统计（0.5分段）。"""
    from app.models import UserVideoFinalScore
    rows = UserVideoFinalScore.query.filter(
        UserVideoFinalScore.score > 0
    ).all()
    scores = [float(r.score) for r in rows]
    if not scores:
        return {'count': 0, 'avg': None, 'min': None, 'max': None, 'distribution': []}
    from collections import Counter as _Cnt
    def _bucket(v):
        return round(round(v * 2) / 2, 1)   # 四舍五入到0.5
    dist = _Cnt(_bucket(s) for s in scores)
    return {
        'count':        len(scores),
        'avg':          round(sum(scores) / len(scores), 4),
        'min':          round(min(scores), 2),
        'max':          round(max(scores), 2),
        'distribution': [
            {'score': k, 'count': dist[k]}
            for k in sorted(dist.keys())
        ],
    }


# ─────────────────────────────────────────────────
# 积分结算接口
# ─────────────────────────────────────────────────

@api.route('/teacher/point_settle', methods=['POST'])
@login_required
@_teacher_required_api
@_adv_test_readonly_api
def point_settle():
    """
    积分结算：计算视频金银铜牌奖励、教师评分最接近奖励、守擂/打擂奖励。

    结算规则（按视频批次，防止重复）：
    - 对每道题的所有作业视频：找评分≥1人的视频按均分排名
      - 第1名：金牌 +5；第2名：银牌 +4；第3名：铜牌 +3
    - 对每个视频：找与教师评分最接近的同学 +2（教师账号打分）
    - Champion 守擂/打擂：Champion 表有记录 → 对应用户若未有 champion_defend/win 积分则补记
    - 每个 (video_id, award_type) 只结算一次（靠 PointSettlement 去重）

    返回：{ log: [...], awarded: N }
    """
    from app.models import (Video, VideoRating, Champion,
                            PointLog, PointSettlement)
    from sqlalchemy import func as sqlfunc
    from collections import defaultdict

    log_lines = []
    awarded   = 0

    # ── 1. 金银铜牌结算（全局 Top-3）────────────────────────────
    # 所有作业视频按平均评分降序，全局取前3
    # 去重修复说明：原代码用 filter_by(award_type=award_type) 全局查找，
    # 导致只要任意视频曾获某牌型，后续所有结算均跳过该牌型。
    # 修正为 filter_by(video_id=video.id, award_type=award_type)，
    # 按视频独立去重，不同视频可以各自获得同类型奖牌。
    global_ranked = db.session.query(
        Video,
        sqlfunc.avg(VideoRating.value).label('avg_r'),
        sqlfunc.count(VideoRating.id).label('cnt_r')
    ).join(VideoRating, VideoRating.video_id == Video.id)\
     .filter(Video.type == 'homework')\
     .group_by(Video.id)\
     .having(sqlfunc.count(VideoRating.id) >= 1)\
     .order_by(sqlfunc.avg(VideoRating.value).desc())\
     .all()

    medal_map = {0: ('medal_gold', 5.0, '金牌'),
                 1: ('medal_silver', 4.0, '银牌'),
                 2: ('medal_bronze', 3.0, '铜牌')}

    for rank, (video, avg_r, cnt_r) in enumerate(global_ranked[:3]):
        award_type = medal_map[rank][0]
        pts        = medal_map[rank][1]
        label      = medal_map[rank][2]

        # 去重：每个 (video_id, award_type) 只结算一次
        already = PointSettlement.query.filter_by(
            video_id=video.id, award_type=award_type
        ).first()
        if already:
            continue

        user = video.author
        if not user:
            continue

        user.reward_points = (user.reward_points or 0) + pts
        db.session.add(PointLog(
            user_id=user.id, points=pts,
            reason=award_type, ref_video_id=video.id,
            memo=f'全局{label} 均分{round(float(avg_r),1)}（{video.title}）'
        ))
        db.session.add(PointSettlement(
            video_id=video.id, award_type=award_type,
            settled_by=current_user.id
        ))
        log_lines.append(
            f'[{label}] {user.username} 《{video.title}》 '
            f'均分{round(float(avg_r),1)} +{pts}分'
        )
        awarded += 1

    # ── 2. 与教师评分最接近奖励 ─────────────────────────────
    # 找出教师账号
    teachers = User.query.filter_by(is_teacher=True).all()
    teacher_ids = {t.id for t in teachers}

    if teacher_ids:
        # 获取所有教师对视频的评分
        teacher_ratings = VideoRating.query.filter(
            VideoRating.user_id.in_(teacher_ids)
        ).all()

        # video_id → 教师评分均值
        t_rating_map = defaultdict(list)
        for tr in teacher_ratings:
            t_rating_map[tr.video_id].append(tr.value)
        t_avg_map = {vid: sum(vals)/len(vals) for vid, vals in t_rating_map.items()}

        for video_id, teacher_avg in t_avg_map.items():
            # 已结算过的跳过
            already = PointSettlement.query.filter_by(
                video_id=video_id, award_type='closest_score'
            ).first()
            if already:
                continue

            # 找该视频所有学生评分，找差值最小的
            student_ratings = VideoRating.query.filter(
                VideoRating.video_id == video_id,
                VideoRating.user_id.notin_(teacher_ids)
            ).all()
            if not student_ratings:
                continue

            best_rating = min(student_ratings, key=lambda r: abs(r.value - teacher_avg))
            min_diff    = abs(best_rating.value - teacher_avg)

            # 只奖励差值 ≤ 1.5 的（防止教师没打分时乱奖励）
            if min_diff > 1.5:
                continue

            user = User.query.get(best_rating.user_id)
            if not user:
                continue

            # 上限：每人最多累积 5 次最接近奖励，超出不再发放
            existing_count = PointLog.query.filter_by(
                user_id=user.id, reason='closest_score'
            ).count()
            if existing_count >= 5:
                continue

            video = Video.query.get(video_id)
            user.reward_points = (user.reward_points or 0) + 1.0
            db.session.add(PointLog(
                user_id=user.id, points=1.0,
                reason='closest_score', ref_video_id=video_id,
                memo=f'评分{best_rating.value}最接近教师{round(teacher_avg,1)} 差{round(min_diff,2)}'
            ))
            db.session.add(PointSettlement(
                video_id=video_id, award_type='closest_score',
                settled_by=current_user.id
            ))
            title = video.title if video else f'视频#{video_id}'
            log_lines.append(
                f'[最近评分] {user.username} 《{title}》 '
                f'评分{best_rating.value}≈教师{round(teacher_avg,1)} +2分'
            )
            awarded += 1

    # ── 3. 守擂/打擂积分补记 ────────────────────────────────
    # Champion 表记录了当前擂主，结算时给从未记录过 champion_defend 的擂主补记一次
    champions = Champion.query.all()
    for champ in champions:
        already = PointSettlement.query.filter_by(
            video_id=champ.video_id, award_type='champion'
        ).first()
        if already:
            continue

        user = User.query.get(champ.user_id)
        if not user:
            continue

        user.reward_points = (user.reward_points or 0) + 2.0
        db.session.add(PointLog(
            user_id=user.id, points=2.0,
            reason='champion_defend', ref_video_id=champ.video_id,
            memo=f'守擂 {champ.problem_key}'
        ))
        db.session.add(PointSettlement(
            video_id=champ.video_id, award_type='champion',
            settled_by=current_user.id
        ))
        log_lines.append(
            f'[守擂] {user.username} {champ.problem_key} +2分'
        )
        awarded += 1

    # ── 4. 有意义回复统计（更新 user.meaningful_replies_count）───
    # 对所有有评论的学生：用 AI 重新评估未判定的评论，再汇总计数
    #
    # 注意：此步骤涉及大量 DeepSeek API 调用（可能几百到上千条评论），
    # 单次 HTTP 请求无法在超时前完成。因此改为后台线程异步执行，
    # 前端通过 /api/teacher/point_settle 的 GET 方法轮询进度。

    def _classify_comments_bg(app_object):
        with app_object.app_context():
            import unicodedata
            from sqlalchemy import select as _sel

            _MIN_CN_CHARS = 20
            from datetime import datetime as _dt
            # 三重否决生效起点（此前的评论保留历史判定）
            try:
                _WATCH_SINCE = _dt.strptime(
                    app_object.config.get('WATCH_TRACKING_START', '2026-07-27'), '%Y-%m-%d')
            except Exception:
                _WATCH_SINCE = _dt(2026, 7, 27)

            def _cn_char_count(text: str) -> int:
                return sum(1 for c in text if unicodedata.category(c).startswith('Lo'))

            from app.models import VideoComment as _VCM, VideoAIScore as _VAS, VideoWatchRecord as _VWR
            from app.utils.ai_client import classify_reply as _classify_reply
            from app.utils.ai_client import classify_reply_with_transcript as _classify_with_ts
            from datetime import timedelta as _td
            _TEN_MIN = _td(minutes=10)

            all_students = User.query.filter_by(is_teacher=False).all()
            total_students = len(all_students)
            for idx, stu in enumerate(all_students):
                # 更新进度
                try:
                    _job = AIScoringJob.get_singleton()
                    _job.current   = f'评论审核: {stu.username} ({idx+1}/{total_students})'
                    _job.processed = idx + 1
                    _job.total     = total_students
                    db.session.commit()
                except Exception:
                    pass  # 写状态失败不中断

                # 直接用 select 查 own video ids，避免 subquery 引发 flush
                own_rows = db.session.execute(
                    _sel(Video.id).where(Video.user_id == stu.id)
                ).fetchall()
                own_ids = {r[0] for r in own_rows} if own_rows else set()
                if own_ids:
                    user_cmts = _VCM.query.filter(
                        _VCM.user_id == stu.id,
                        ~_VCM.video_id.in_(own_ids)
                    ).all()
                else:
                    user_cmts = _VCM.query.filter_by(user_id=stu.id).all()

                # 批量预取本学生评论涉及的 视频/转译/观看记录，避免 N+1
                _vids = {c.video_id for c in user_cmts}
                _vmap = {v.id: v for v in Video.query.filter(Video.id.in_(_vids)).all()} if _vids else {}
                _amap = {a.video_id: a for a in _VAS.query.filter(_VAS.video_id.in_(_vids)).all()} if _vids else {}
                _wmap = {w.video_id: w for w in _VWR.query.filter(_VWR.video_id.in_(_vids), _VWR.user_id == stu.id).all()} if _vids else {}

                for cmt in user_cmts:
                    if _cn_char_count(cmt.content) < _MIN_CN_CHARS:
                        cmt.is_meaningful = False
                        cmt.reject_reason = 'short'
                        continue
                    video = _vmap.get(cmt.video_id)
                    # 仅作业视频走三重否决；非作业视频维持原 AI 判定
                    if not video or video.type != 'homework':
                        if cmt.is_meaningful:
                            try:
                                cmt.is_meaningful = _classify_reply(cmt.content)
                            except Exception:
                                pass
                        cmt.reject_reason = None
                        continue
                    # 作业视频：三重否决
                    # 三重否决生效起点之前的评论保留历史判定（彼时无观看追踪数据，不追溯）
                    if cmt.created_at and cmt.created_at < _WATCH_SINCE:
                        continue
                    ai = _amap.get(video.id)
                    if not ai or not ai.transcript:
                        cmt.is_meaningful = False
                        cmt.reject_reason = 'no_transcript'
                        continue
                    if ai.checkpoint_sec is None:
                        # 已转译但无检查点（视频过短或尚未回填）：仅做 AI 语义+质量判定
                        try:
                            ok, reason = _classify_with_ts(cmt.content, ai.transcript)
                            cmt.is_meaningful = ok
                            cmt.reject_reason = None if ok else (reason or 'ai')
                        except Exception:
                            cmt.reject_reason = 'ai_error'
                        continue
                    rec = _wmap.get(video.id)
                    # 解析历次覆盖检查点的观看时刻列表
                    try:
                        _times = [_dt.fromisoformat(t) for t in json.loads(
                            rec.watch_times_json)] if rec and rec.watch_times_json else []
                    except Exception:
                        _times = []
                    # 兼容旧记录：watch_times_json 为空但 watch_time 有值
                    if not _times and rec and rec.watch_time:
                        _times = [rec.watch_time]
                    if not _times:                     # 否决1：从未看过检查点
                        cmt.is_meaningful = False
                        cmt.reject_reason = 'watch'
                        continue
                    # 否决2：评论需在某次观看后 10 分钟内（与它之前最近一次观看比，老评论判定稳定）
                    if not any(t <= cmt.created_at <= t + _TEN_MIN for t in _times):
                        cmt.is_meaningful = False
                        cmt.reject_reason = 'temporal'
                        continue
                    # 否决3 + 质量：AI（带转译文本）
                    try:
                        ok, reason = _classify_with_ts(cmt.content, ai.transcript)
                        cmt.is_meaningful = ok
                        cmt.reject_reason = None if ok else (reason or 'ai')
                    except Exception:
                        cmt.reject_reason = 'ai_error'

                meaningful_cnt = sum(
                    1 for c in user_cmts
                    if c.is_meaningful and _cn_char_count(c.content) >= _MIN_CN_CHARS
                )
                stu.meaningful_replies_count = meaningful_cnt

                # 每个学生提交一次，避免 SQLite 锁累积
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            _job = AIScoringJob.get_singleton()
            _job.status  = 'done'
            _job.current = ''
            db.session.commit()

    # 捕获 app 对象（不能在后台线程里用 current_app proxy）
    _app_obj = current_app._get_current_object()

    # 复用 AIScoringJob 表来追踪进度（注意：与 AI 评分任务共用此表，
    # 但不能互相冲突；评论审核开始前先确认前一任务已完成）
    _job = AIScoringJob.get_singleton()
    _job.status    = 'running'
    _job.total     = User.query.filter_by(is_teacher=False).count()
    _job.processed = 0
    _job.failed    = 0
    _job.current   = '正在启动评论审核…'
    _job.error_msg = ''
    db.session.commit()

    comment_thread = threading.Thread(
        target=_classify_comments_bg, args=(_app_obj,), daemon=True
    )
    comment_thread.start()

    log_lines.append('💬 评论审核已在后台启动（共需处理 {} 名学生的评论），完成后自动更新回复统计'.format(_job.total))

    if len(log_lines) == 1 and log_lines[0] == '':
        log_lines[0] = '✅ 本次无新增结算项（所有已计算的视频均已记录，无重复）'

    return jsonify({
        'ok':              True,
        'awarded':         awarded,
        'log':             log_lines,
        'comment_job_total': _job.total if _job.status == 'running' else 0,
    })


# ── AI 评分手动触发接口 ──────────────────────────────────────────

@api.route('/teacher/recalc_ai_scores', methods=['POST'])
@login_required
@_teacher_required_api
@_adv_test_readonly_api
def recalc_ai_scores():
    """
    手动对所有尚无 AI 评分的作业视频触发评分流水线。
    流水线：提取 WAV → 阿里云 ASR → DeepSeek 打分 → 写入 video_ai_score 表。

    后台异步执行，立即返回。前端通过 GET /api/teacher/recalc_ai_scores 轮询进度。
    """

    job = AIScoringJob.get_singleton()
    if job.status == 'running':
        return jsonify({
            'ok': True,
            'message': '已有评分任务正在运行中，请等待完成后再触发',
            'status': 'running',
            'total': job.total,
            'processed': job.processed,
            'failed': job.failed,
        })

    # 计算待处理列表
    scored_ids = {r.video_id for r in VideoAIScore.query.all()}
    videos = Video.query.filter(
        Video.type == 'homework',
        ~Video.id.in_(scored_ids)
    ).all() if scored_ids else Video.query.filter_by(type='homework').all()

    if not videos:
        return jsonify({'ok': True, 'message': '所有视频已有 AI 评分，无需处理', 'total': 0})

    # 标记任务开始
    job.status    = 'running'
    job.total     = len(videos)
    job.processed = 0
    job.failed    = 0
    job.current   = ''
    job.error_msg = ''
    db.session.commit()

    # 把待处理视频 ID 列表和当前 app 引用传给后台线程
    video_ids = [v.id for v in videos]
    app_ref   = current_app._get_current_object()

    def _process_all():
        import subprocess, os, json as _json, re as _re
        from pathlib import Path as _Path

        UPLOAD_FOLDER = app_ref.config.get(
            'UPLOAD_FOLDER', os.path.join(app_ref.root_path, 'static', 'uploads')
        )

        def _extract_wav(video_path):
            wav = video_path + '.tmp_asr.wav'
            r = subprocess.run(
                ['ffmpeg', '-i', video_path,
                 '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', wav],
                capture_output=True, timeout=120
            )
            return wav if r.returncode == 0 and os.path.exists(wav) else None

        def _transcribe(wav_path):
            import os as _os
            api_key = _os.environ.get('ALIYUN_DASHSCOPE_KEY', '').strip()
            if not api_key:
                kf = _Path('/app/AliyunKey.txt')
                api_key = kf.read_text(encoding='utf-8').strip() if kf.exists() else ''
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
                resp = recognition.call(wav_path)
                if resp.status_code == 200:
                    sentences = (resp.output or {}).get('sentence', [])
                    return ' '.join(s.get('text', '') for s in sentences).strip()
                else:
                    print(f'    [ASR] 请求失败: {resp.status_code} {getattr(resp, "message", "")}', flush=True)
            except Exception as e:
                print(f'    [ASR] 异常: {e}', flush=True)
            return None

        # ── 题目-知识点映射（按题号精确匹配，与 run_ai_scoring.py 一致）──
        def _load_kp_map():
            from app.utils.course_paths import qg_path as _qg_path_func
            qg_path = _qg_path_func()
            if not qg_path.exists():
                return {}
            root = _json.loads(qg_path.read_text(encoding='utf-8'))
            kp_map = {}
            for chap in root.get('children', []):
                m = _re.search(r'\d+', chap.get('id', ''))
                if not m:
                    continue
                ch = int(m.group())
                q_map = {}
                for section in chap.get('children', []):
                    label = str(section.get('label', '')).strip()
                    sm = _re.search(r'(\d+)\s*[-－]\s*(\d+)', label)
                    if not sm:
                        sm = _re.search(r'^(\d+)\D+(\d+)$', label)
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

        def _resolve_problem_no(video):
            if getattr(video, 'problem_no', None):
                return int(video.problem_no)
            title = getattr(video, 'title', '') or ''
            for pat in [r'第\s*(\d+)\s*章\s*第?\s*(\d+)\s*题',
                        r'(\d+)\s*章\s*(\d+)\s*题',
                        r'(\d+)\s*[-－]\s*(\d+)']:
                m = _re.search(pat, title)
                if m:
                    return int(m.group(2))
            return None

        def _get_problem_kps(chapter, problem_no):
            if not chapter or not problem_no:
                return []
            return KP_MAP.get(chapter, {}).get(problem_no, [])

        def _clamp_half(value, low, high):
            value = max(low, min(high, value))
            return round(value * 2) / 2

        def _calc_final_score(expression_score, kp_scores, has_exact_match):
            expression_score = _clamp_half(expression_score, 0.0, 2.0)
            if has_exact_match and kp_scores:
                kp_total = sum(_clamp_half(float(s.get('score', 0) or 0), 0.0, 1.0)
                               for s in kp_scores)
                kp_component = 3.0 * (kp_total / len(kp_scores))
            else:
                kp_component = 0.0
            score = 5.0 + expression_score + kp_component
            score = max(6.0, min(10.0, score))
            return round(score * 2) / 2

        def _fallback_score(transcript):
            length = len((transcript or '').strip())
            if length >= 900:
                expression_score = 2.0; reason = '讲解完整度较高，但未命中题目图谱'
            elif length >= 500:
                expression_score = 2.0; reason = '讲解较完整，但未命中题目图谱'
            elif length >= 180:
                expression_score = 1.5; reason = '讲解基本完整，但未命中题目图谱'
            else:
                expression_score = 1.0; reason = '讲解较简略，且未命中题目图谱'
            score = max(6.0, min(7.5, round((5.0 + expression_score) * 2) / 2))
            return score, reason, []

        def _json_block(text):
            if not text:
                return None
            text = text.strip()
            if text.startswith('{') and text.endswith('}'):
                return text
            m = _re.search(r'\{[\s\S]*\}', text)
            return m.group(0) if m else None

        _STRUCTURED_PROMPT = (
            "你是一位大学力学课程的视频作业评审专家。请只做结构化判定，不要自己计算总分。\n\n"
            "【视频信息】\n"
            "- 标题：{title}\n"
            "- 章节：第{chapter}章\n"
            "- 题号：第{problem_no}题\n"
            "- 是否命中题目图谱：{has_exact_match}\n\n"
            "【本题对应知识点】\n"
            "{kp_lines}\n\n"
            "【视频讲解文字转写】\n"
            "{transcript}\n\n"
            "【判定规则】\n"
            "1. 你只输出 JSON，不要输出任何额外说明。\n"
            "2. expression_score 只能是 0、0.5、1、1.5、2 之一。\n"
            "3. 每个知识点的 score 只能是 0、0.5、1 之一。\n"
            "4. 0 分：未提及，也没有相关推导痕迹。\n"
            "5. 0.5 分：疑似提及；或被 ASR 谐音/错词污染但上下文可判断；"
            "或属于公式/方程/字母符号类知识点，虽然名称没转对，"
            "但出现了明显代号、代入、变形、单位或计算链条。\n"
            "6. 1 分：明确提及且讲清楚了作用、条件、公式意义或使用过程。\n"
            "7. 如果没有命中题目图谱，请返回空的 kp_scores 列表，"
            "并只根据讲解完整度、逻辑性、术语准确度给 expression_score 与 overall_reason。\n"
            "8. overall_reason 控制在 40 字以内。\n\n"
            "请严格输出如下 JSON：\n"
            "{{\n"
            '  "expression_score": 0,\n'
            '  "overall_reason": "",\n'
            '  "kp_scores": [\n'
            '    {{"kp": "知识点名", "score": 0, "evidence": "不超过18字"}}\n'
            '  ]\n'
            "}}"
        )

        def _ai_score(video, transcript):
            import os as _os
            from openai import OpenAI as _OAI

            chapter = getattr(video, 'chapter', None)
            problem_no = _resolve_problem_no(video)
            kps = _get_problem_kps(chapter, problem_no)
            has_exact_match = bool(kps)
            kp_lines = '\n'.join(f'- {i}. {k}' for i, k in enumerate(kps, 1)) \
                       if kps else '（未命中题目图谱）'

            api_key_deepseek = _os.environ.get('DEEPSEEK_API_KEY', '').strip()
            if not api_key_deepseek:
                kf = _Path('/app/APIKey.txt')
                api_key_deepseek = kf.read_text(encoding='utf-8').strip() if kf.exists() else ''
            if not api_key_deepseek:
                return None, '', []

            prompt = _STRUCTURED_PROMPT.format(
                title=video.title or '?',
                chapter=chapter or '?',
                problem_no=problem_no or '?',
                has_exact_match='是' if has_exact_match else '否',
                kp_lines=kp_lines,
                transcript=(transcript or '')[:4000],
            )
            try:
                client = _OAI(api_key=api_key_deepseek, base_url='https://api.deepseek.com')
                resp = client.chat.completions.create(
                    model='deepseek-chat',
                    messages=[{'role': 'user', 'content': prompt}],
                    temperature=0, max_tokens=700,
                )
                text = resp.choices[0].message.content.strip()
                raw_json = _json_block(text)
                if not raw_json:
                    if not has_exact_match:
                        return _fallback_score(transcript)
                    return None, '', []

                data = _json.loads(raw_json)
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
                    by_name = {s['kp']: s for s in kp_scores}
                    kp_scores = [by_name.get(k, {'kp': k, 'score': 0.0, 'evidence': ''})
                                 for k in kps]
                    score = _calc_final_score(expression_score, kp_scores, True)
                else:
                    score, fallback_reason, _kp_scores = _fallback_score(transcript)
                    if not reason:
                        reason = fallback_reason

                if not reason:
                    reason = '结构化评分完成'
                return score, reason, kp_scores
            except Exception:
                pass

            if not has_exact_match:
                return _fallback_score(transcript)
            return None, '', []

        with app_ref.app_context():
            from app.models import AIScoringJob as _Job

            for vid in video_ids:
                job_rec = _Job.get_singleton()
                if job_rec.status != 'running':
                    # 外部中止
                    break

                video = Video.query.get(vid)
                if video is None:
                    job_rec.failed += 1
                    db.session.commit()
                    continue

                job_rec.current = video.title or str(vid)
                db.session.commit()

                basename   = os.path.basename(video.url)
                video_path = os.path.join(UPLOAD_FOLDER, basename)
                if not os.path.exists(video_path):
                    job_rec.failed += 1
                    job_rec.current = ''
                    db.session.commit()
                    continue

                wav_path = None
                try:
                    wav_path = _extract_wav(video_path)
                    if not wav_path:
                        job_rec.failed += 1
                        db.session.commit()
                        continue

                    transcript = _transcribe(wav_path)
                    if not transcript:
                        job_rec.failed += 1
                        db.session.commit()
                        continue

                    score, reason, kps_info = _ai_score(video, transcript)
                    if score is None:
                        job_rec.failed += 1
                        db.session.commit()
                        continue

                    ai_rec = VideoAIScore(
                        video_id   = video.id,
                        score      = score,
                        transcript = transcript,
                        kp_matched = _json.dumps(kps_info, ensure_ascii=False),
                        reason     = reason,
                    )
                    db.session.add(ai_rec)
                    db.session.commit()
                    job_rec.processed += 1
                except Exception:
                    job_rec.failed += 1
                finally:
                    if wav_path and os.path.exists(wav_path):
                        os.remove(wav_path)
                    job_rec.current = ''
                    db.session.commit()

            # 标记完成
            final_job = _Job.get_singleton()
            if final_job.status == 'running':
                final_job.status  = 'done'
                final_job.current = ''
                db.session.commit()

    thread = threading.Thread(target=_process_all, daemon=True)
    thread.start()

    return jsonify({
        'ok': True,
        'message': f'评分任务已在后台启动，共 {len(videos)} 个视频待处理',
        'total': len(videos),
        'status': 'running',
    })


@api.route('/teacher/recalc_ai_scores', methods=['GET'])
@login_required
@_teacher_required_api
def recalc_ai_scores_status():
    """查询 AI 评分任务进度"""
    job = AIScoringJob.get_singleton()
    return jsonify({
        'status':    job.status,
        'total':     job.total,
        'processed': job.processed,
        'failed':    job.failed,
        'current':   job.current or '',
        'error_msg': job.error_msg or '',
    })
