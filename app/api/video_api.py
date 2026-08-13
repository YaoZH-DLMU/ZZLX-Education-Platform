from flask import jsonify, request, current_app, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename
from sqlalchemy import func, or_
import os
import json
from datetime import datetime
from . import api
from ..models import Video, VideoComment, VideoFavorite, VideoRating, User, db, send_notification, VideoWatchRecord, VideoAIScore
from ..utils.ai_client import async_classify
import time
import subprocess

ALLOWED_EXTENSIONS = {'mp4', 'webm', 'ogg'}

from ..utils.visibility import hide_test_content as _hide_test_content

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _auto_update_champion(video):
    """评分后自动更新擂主（满足门槛时，最高分作者自动成为该题擂主）。"""
    if not (video.type == 'homework' and video.chapter and video.problem_no):
        return
    from ..models import Champion

    problem_key = f"{video.chapter}-{video.problem_no}"

    # 门槛校验
    rating_count  = VideoRating.query.filter_by(video_id=video.id).count()
    comment_count = VideoComment.query.filter_by(video_id=video.id).count()
    if rating_count < 5 or comment_count < 3:
        return

    # 必须有至少一位教师评过分
    from ..models import User as _User
    teacher_ids = [u.id for u in _User.query.filter_by(is_teacher=True).all()]
    if not teacher_ids:
        return
    teacher_rated = VideoRating.query.filter(
        VideoRating.video_id == video.id,
        VideoRating.user_id.in_(teacher_ids)
    ).first()
    if not teacher_rated:
        return

    # 判断该视频是否为该题当前最高分
    best = db.session.query(Video)\
        .outerjoin(VideoRating, VideoRating.video_id == Video.id)\
        .filter(
            Video.type == 'homework',
            Video.chapter == video.chapter,
            Video.problem_no == video.problem_no,
        )\
        .group_by(Video.id)\
        .order_by(func.avg(VideoRating.value).desc().nullslast())\
        .first()

    if not best or best.id != video.id:
        return

    # 更新或创建擂主记录
    existing = Champion.query.filter_by(problem_key=problem_key).first()
    if existing:
        if existing.video_id == video.id and existing.user_id == video.user_id:
            return  # 已正确，无需更新
        existing.user_id     = video.user_id
        existing.video_id    = video.id
        existing.declared_at = db.func.now()
    else:
        current_slots = Champion.query.filter_by(user_id=video.user_id).count()
        if current_slots >= 5:
            return  # 席位已满，不自动扩增
        db.session.add(Champion(
            problem_key=problem_key,
            user_id=video.user_id,
            video_id=video.id,
        ))


def _merge_intervals(a_list, b_list):
    """合并两组 [[start,end],...] 区间，返回合并重叠/相邻后的升序列表。"""
    pts = sorted(list(a_list) + list(b_list))
    merged = []
    for s, e in pts:
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)
    return merged


def _intervals_cover(intervals, a, b):
    """判断区间并集是否完整覆盖 [a, b]。intervals 需已合并排序。"""
    cov = a
    for s, e in intervals:
        if s > cov:
            return False            # 出现缺口
        if e >= b:
            return True
        cov = max(cov, e)
    return cov >= b


@api.route('/videos/<int:video_id>/watch', methods=['POST'])
@login_required
def report_watch(video_id):
    """接收学生前端上报的 1x 观看区间，合并存入 VideoWatchRecord。
    若已覆盖检查点窗口 [cp-5, cp+5] 且 watch_time 未设，则记录首次覆盖时刻。"""
    video = Video.query.get_or_404(video_id)
    if video.type != 'homework':
        return jsonify({'success': False, 'error': '仅作业视频记录观看'}), 400
    data = request.get_json(silent=True) or {}
    raw = data.get('intervals', [])
    new_iv = []
    for pair in raw:
        try:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                s, e = float(pair[0]), float(pair[1])
                if e > s:
                    new_iv.append([s, e])
        except Exception:
            pass
    if not new_iv:
        return jsonify({'success': True, 'merged': []})

    rec = VideoWatchRecord.query.filter_by(video_id=video_id, user_id=current_user.id).first()
    try:
        old = json.loads(rec.intervals_json) if rec and rec.intervals_json else []
    except Exception:
        old = []
    merged = _merge_intervals(old, new_iv)

    # 检查点覆盖判定：覆盖则记一个观看时刻到 watch_times（去重60s、留最近200条）
    ai = VideoAIScore.query.filter_by(video_id=video_id).first()
    cp = ai.checkpoint_sec if ai and ai.checkpoint_sec else None
    now = datetime.utcnow()
    covered = cp and _intervals_cover(merged, cp - 5, cp + 5)
    try:
        times = json.loads(rec.watch_times_json) if rec and rec.watch_times_json else []
    except Exception:
        times = []
    if covered:
        # 与最近一条间隔 >60s 才追加（同一观看会话不重复记）
        if not times or (now - datetime.fromisoformat(times[-1])).total_seconds() > 60:
            times.append(now.isoformat())
            times = times[-200:]   # 留最近200条
    watch_time = now if covered else (rec.watch_time if rec else None)

    if rec:
        rec.intervals_json = json.dumps(merged)
        rec.watch_time = watch_time
        rec.watch_times_json = json.dumps(times)
    else:
        rec = VideoWatchRecord(video_id=video_id, user_id=current_user.id,
                               intervals_json=json.dumps(merged), watch_time=watch_time,
                               watch_times_json=json.dumps(times))
        db.session.add(rec)
    db.session.commit()
    return jsonify({'success': True, 'merged': merged, 'watch_time': watch_time.isoformat() if watch_time else None, 'watch_times': len(times)})


@api.route('/videos', methods=['GET'])
def get_videos():
    video_type = request.args.get('type', 'homework')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    videos = Video.query.filter_by(type=video_type).paginate(page=page, per_page=per_page)
    
    return jsonify({
        'videos': [{
            'id': v.id,
            'title': v.title,
            'description': v.description,
            'url': v.url,
            'views': v.views,
            'author': v.user.username
        } for v in videos.items],
        'total': videos.total,
        'pages': videos.pages,
        'current_page': videos.page
    })

@api.route('/videos/upload', methods=['POST'])
@login_required
def upload_video():
    if 'video' not in request.files:
        return jsonify({'message': '没有上传文件'}), 400

    file = request.files['video']
    if not file or not allowed_file(file.filename):
        return jsonify({'message': '不支持的文件格式'}), 400

    filename = secure_filename(file.filename)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    new_filename = f"{current_user.id}_{timestamp}_{filename}"

    # 保存文件
    from app.utils.course_paths import upload_folder as _uf
    file_path = os.path.join(_uf(), new_filename)
    file.save(file_path)

    video_type = request.form.get('video_type', 'homework')  # 'homework' | 'flipped'

    # 自动生成标题
    if video_type == 'homework':
        subject = request.form.get('subject', '')
        chapter = request.form.get('chapter', '')
        problem_no = request.form.get('problem_no', '')
        title = f"{subject}{chapter}章{problem_no}题"
    else:
        group_no = request.form.get('group_no', '')
        class_no = request.form.get('class_no', '')
        if str(group_no) == '15':
            title = f"第{class_no}次翻转课堂教师总结"
        else:
            title = f"第{group_no}小组第{class_no}次翻转课堂"

    # 创建视频记录
    video = Video(
        title=title,
        url=url_for('static', filename=f'uploads/{new_filename}'),
        type=video_type,
        subject=request.form.get('subject'),
        chapter=request.form.get('chapter', type=int),
        problem_no=request.form.get('problem_no', type=int),
        group_no=request.form.get('group_no', type=int),
        class_no=request.form.get('class_no', type=int),
        user_id=current_user.id
    )
    db.session.add(video)
    db.session.commit()

    # 超过 20 MB 时后台自动压缩（替换原文件，文件名不变）
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if file_size_mb > 20:
        import threading, logging
        _logger = logging.getLogger('video_compress')
        def _compress(path):
            tmp = path + '.compressing.mp4'
            _logger.warning('[compress] START %s  (%.1f MB)', path, os.path.getsize(path)/(1024*1024))
            try:
                result = subprocess.run(
                    ['ffmpeg', '-i', path,
                     '-c:v', 'libx264', '-crf', '28', '-preset', 'medium',
                     '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
                     '-threads', '2', '-y', tmp],
                    capture_output=True, text=True,
                    timeout=600
                )
                if result.returncode == 0 and os.path.exists(tmp):
                    orig_mb = os.path.getsize(path) / (1024 * 1024)
                    new_mb  = os.path.getsize(tmp)  / (1024 * 1024)
                    os.replace(tmp, path)
                    _logger.warning('[compress] OK %s  %.1fMB -> %.1fMB', path, orig_mb, new_mb)
                else:
                    _logger.error('[compress] FAIL rc=%s\nstdout=%s\nstderr=%s',
                                  result.returncode, result.stdout[-500:], result.stderr[-500:])
            except Exception as e:
                _logger.exception('[compress] EXCEPTION %s: %s', path, e)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
        threading.Thread(target=_compress, args=(file_path,), daemon=True).start()
        _logger.warning('[compress] thread launched for video_id=%s  path=%s', video.id, file_path)

    return jsonify({'message': '上传成功', 'video_id': video.id})

@api.route('/videos/<int:video_id>/rate', methods=['POST'])
@login_required
def rate_video(video_id):
    rating_value = request.json.get('rating')
    if not 1 <= rating_value <= 10:
        return jsonify({'message': '评分必须在1-10之间'}), 400
        
    rating = VideoRating.query.filter_by(
        user_id=current_user.id,
        video_id=video_id
    ).first()
    
    if rating:
        rating.value = rating_value
    else:
        rating = VideoRating(
            user_id=current_user.id,
            video_id=video_id,
            value=rating_value
        )
        db.session.add(rating)
    
    db.session.commit()

    # 自动更新擂主（满足门槛时无需手动守擂）
    video = Video.query.get_or_404(video_id)
    try:
        _auto_update_champion(video)
        db.session.commit()
    except Exception:
        db.session.rollback()

    # 返回更新后的统计信息
    return jsonify({
        'avg_rating': video.average_rating,
        'favorites_count': video.favorites_count,
        'comments_count': video.comments_count
    })

@api.route('/videos/<int:video_id>/favorite', methods=['POST'])
@login_required
def toggle_favorite(video_id):
    favorite = VideoFavorite.query.filter_by(
        user_id=current_user.id,
        video_id=video_id
    ).first()
    
    if favorite:
        db.session.delete(favorite)
        is_favorited = False
    else:
        favorite = VideoFavorite(user_id=current_user.id, video_id=video_id)
        db.session.add(favorite)
        is_favorited = True
    
    db.session.commit()
    
    video = Video.query.get_or_404(video_id)
    if is_favorited and video.user_id != current_user.id:
        chapter_url = f'/homework/chapter/{video.chapter}' if video.chapter else '/homework'
        send_notification(
            user_id=video.user_id,
            title='你的视频收到新收藏',
            content=f'{current_user.username} 收藏了《{video.title}》',
            ntype='new_favorite',
            link=f'{chapter_url}?video_id={video_id}',
            sender_id=current_user.id
        )
        db.session.commit()
    return jsonify({
        'is_favorited': is_favorited,
        'favorites_count': video.favorites_count
    })

@api.route('/videos/<int:video_id>/comments', methods=['POST'])
@login_required
def add_comment(video_id):
    content = request.json.get('content')
    if not content:
        return jsonify({'message': '评论内容不能为空'}), 400
        
    comment = VideoComment(
        content=content,
        user_id=current_user.id,
        video_id=video_id
    )
    
    db.session.add(comment)
    db.session.commit()

    # 通知视频作者有新评论
    video = Video.query.get(video_id)
    if video and video.user_id != current_user.id:
        chapter_url = f'/homework/chapter/{video.chapter}' if video.chapter else '/homework'
        send_notification(
            user_id=video.user_id,
            title='你的视频收到新评论',
            content=f'{current_user.username} 评论了《{video.title}》：{content[:30]}',
            ntype='new_comment',
            link=f'{chapter_url}?video_id={video_id}',
            sender_id=current_user.id
        )
        db.session.commit()

    # 后台异步判断回复是否有意义
    # 作业视频改由"积分结算"时三重否决判定（观看/时限/语义），不在此实时调；
    # 非作业视频维持实时判定。
    if not video or video.type != 'homework':
        async_classify(current_app._get_current_object(),
                       'VideoComment', comment.id, content)

    return jsonify({
        'comment': {
            'id': comment.id,
            'author': current_user.username,
            'content': comment.content,
            'created_at': comment.created_at.strftime('%Y-%m-%d %H:%M')
        },
        'comments_count': video.comments_count if video else 0
    })

@api.route('/videos/<int:video_id>/comments/<int:comment_id>', methods=['PUT'])
@login_required
def edit_comment(video_id, comment_id):
    """学生修改自己的评论"""
    comment = VideoComment.query.filter_by(id=comment_id, video_id=video_id).first_or_404()
    if comment.user_id != current_user.id:
        return jsonify({'message': '无权编辑此评论'}), 403
    content = (request.get_json(silent=True) or {}).get('content', '').strip()
    if not content:
        return jsonify({'message': '评论内容不能为空'}), 400
    if len(content) > 500:
        return jsonify({'message': '评论内容过长（最多500字）'}), 400
    comment.content = content
    db.session.commit()
    return jsonify({'message': '评论已更新', 'content': comment.content})


# ── 教师屏蔽评论 ──────────────────────────────────────────

@api.route('/videos/<int:video_id>/comments/<int:comment_id>/hide', methods=['POST'])
@login_required
def hide_comment(video_id, comment_id):
    """教师屏蔽/取消屏蔽评论"""
    if not current_user.is_teacher:
        return jsonify({'message': '仅教师可执行此操作'}), 403
    comment = VideoComment.query.filter_by(id=comment_id, video_id=video_id).first_or_404()
    comment.is_hidden = not comment.is_hidden
    db.session.commit()
    action = '屏蔽' if comment.is_hidden else '取消屏蔽'
    return jsonify({'message': f'评论已{action}', 'is_hidden': comment.is_hidden})


@api.route('/videos/<int:video_id>/comments', methods=['GET', 'POST'])
@login_required
def video_comments(video_id):
    if request.method == 'POST':
        content = request.json.get('content')
        comment = VideoComment(content=content, user_id=current_user.id, video_id=video_id)
        db.session.add(comment)
        db.session.commit()
        # 作业视频由积分结算时三重否决判定，非作业视频实时判定
        _v = Video.query.get(video_id)
        if not _v or _v.type != 'homework':
            async_classify(current_app._get_current_object(),
                           'VideoComment', comment.id, content)
        return jsonify({'message': 'Comment added successfully'})

    comments = VideoComment.query.filter_by(video_id=video_id)
    # 非教师不显示被屏蔽的评论
    if not (current_user.is_authenticated and current_user.is_teacher):
        comments = comments.filter_by(is_hidden=False)
    cf = _hide_test_content(VideoComment.user_id)
    if cf is not None:
        comments = comments.filter(cf)
    return jsonify([{
        'id': c.id,
        'content': c.content,
        'author': c.user.username,
        'author_id': c.user_id,
        'is_hidden': c.is_hidden,
        'created_at': c.created_at.isoformat()
    } for c in comments])


@api.route('/videos/<int:video_id>/summary', methods=['GET'])
@login_required
def video_summary(video_id):
    """返回视频有效评论的 AI 总结"""
    from ..utils.ai_client import summarize_replies
    video = Video.query.get_or_404(video_id)
    # 只取 is_meaningful=True 的评论
    useful = VideoComment.query.filter_by(
        video_id=video_id, is_meaningful=True
    ).order_by(VideoComment.created_at.asc()).all()

    if not useful:
        return jsonify({'summary': '暂无有效评论可总结。'})

    texts = [c.content for c in useful]
    context = f'{video.title}（{video.description or ""}）'
    summary = summarize_replies(texts, context)
    return jsonify({'summary': summary})


@api.route('/defense/upload', methods=['POST'])
@login_required
def upload_defense_video():
    """处理翻转课堂视频上传"""
    if 'video' not in request.files:
        return jsonify({'success': False, 'message': '没有选择视频文件'})

    video = request.files['video']
    group_number  = request.form.get('group_number',  type=int)
    defense_order = request.form.get('defense_order', type=int)
    case_type     = request.form.get('case_type', '')   # 'crane' | 'dumper'

    if not all([video, group_number, defense_order]):
        return jsonify({'success': False, 'message': '请填写所有必要信息'})

    try:
        # 根据 case_type 生成标题
        if group_number == 15:
            if case_type == 'crane':
                title = f"双臂立卷夹钳_第{defense_order}次答辩教师总结"
            elif case_type == 'dumper':
                title = f"全机械式翻车机_第{defense_order}次答辩教师总结"
            else:
                title = f"第{defense_order}次答辩教师总结"
        elif case_type == 'crane':
            title = f"双臂立卷夹钳_第{group_number}小组_第{defense_order}次答辩"
        elif case_type == 'dumper':
            title = f"全机械式翻车机_第{group_number}小组_第{defense_order}次答辩"
        else:
            title = f"第{group_number}小组_第{defense_order}次答辩"

        from app.utils.course_paths import upload_folder as _uf
        upload_folder = _uf()
        os.makedirs(upload_folder, exist_ok=True)

        timestamp = int(time.time())
        filename  = secure_filename(f"{current_user.id}_{timestamp}_{video.filename}")
        video_path = os.path.join(upload_folder, filename)
        video.save(video_path)

        # 用 ffmpeg 截取第 0.5 秒封面作为缩略图
        from app.utils.video import generate_thumbnail
        thumbnail_url = generate_thumbnail(video_path, filename, upload_folder)

        new_video = Video(
            title=title,
            url=url_for('static', filename=f'uploads/{filename}'),
            thumbnail=thumbnail_url,
            type='flipped',
            subject=case_type,       # 存储 cage_type 以便后续分组
            group_no=group_number,
            class_no=defense_order,
            user_id=current_user.id,
            views=0,
            created_at=datetime.utcnow()
        )
        db.session.add(new_video)
        db.session.commit()

        return jsonify({'success': True, 'message': '上传成功'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'上传失败：{str(e)}'}) 

# ─────────────────────────────────────────────
# 第三阶段：视频替换 + 重新评分通知
# ─────────────────────────────────────────────

@api.route('/videos/<int:video_id>/replace', methods=['POST'])
@login_required
def replace_video(video_id):
    """替换视频文件，并通知所有曾评分或评论的同学重新评价。

    仅视频上传者本人可替换。替换后：
    - 旧评分记录全部清除（鼓励重新评价新版本）
    - 向所有曾评分 / 评论过该视频的同学发送通知
    """
    from ..models import VideoRating, VideoComment, send_notification

    video = Video.query.get_or_404(video_id)
    if video.user_id != current_user.id:
        return jsonify({'success': False, 'error': '无权替换他人的视频'}), 403

    if 'video' not in request.files:
        return jsonify({'success': False, 'error': '未选择视频文件'}), 400

    new_file = request.files['video']
    if not new_file or not allowed_file(new_file.filename):
        return jsonify({'success': False, 'error': '不支持的文件格式，仅限 mp4/webm/ogg'}), 400

    try:
        # 1. 保存新文件
        upload_folder = _uf()
        timestamp  = int(time.time())
        filename   = secure_filename(f"{current_user.id}_{timestamp}_{new_file.filename}")
        save_path  = os.path.join(upload_folder, filename)
        new_file.save(save_path)

        # 2. 删除旧文件（若存在于 uploads 目录中）
        old_url = video.url  # 如 /static/uploads/xxx.mp4
        if old_url and '/uploads/' in old_url:
            old_filename = old_url.rsplit('/uploads/', 1)[-1]
            old_path = os.path.join(upload_folder, old_filename)
            if os.path.isfile(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass  # 删除失败不阻断流程

        # 3. 更新数据库记录
        video.url = f'/static/uploads/{filename}'

        # 4. 收集需要通知的用户（评分过 + 评论过，排除自己）
        rater_ids   = {r.user_id for r in VideoRating.query.filter_by(video_id=video_id).all()}
        comment_ids = {c.user_id for c in VideoComment.query.filter_by(video_id=video_id).all()}
        notify_ids  = (rater_ids | comment_ids) - {current_user.id}

        # 5. 清除旧评分（新版本应重新评价）
        VideoRating.query.filter_by(video_id=video_id).delete()

        # 6. 发送通知
        for uid in notify_ids:
            send_notification(
                user_id   = uid,
                title     = f'《{video.title}》已更新新版本',
                content   = f'{current_user.username} 更新了他的视频《{video.title}》，请更新评分或评论！',
                ntype     = 'video_replaced',
                link      = f'/video/{video_id}',
                sender_id = current_user.id,
            )

        db.session.commit()
        return jsonify({'success': True, 'message': f'替换成功，已通知 {len(notify_ids)} 位同学'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'替换失败：{str(e)}'}), 500


@api.route('/videos/<int:video_id>', methods=['DELETE'])
@login_required
def delete_video(video_id):
    """教师账号删除视频（含关联评论、收藏、评分）"""
    if not current_user.is_teacher:
        return jsonify({'message': '无权限，仅教师可删除视频'}), 403
    video = Video.query.get_or_404(video_id)
    VideoComment.query.filter_by(video_id=video_id).delete()
    VideoFavorite.query.filter_by(video_id=video_id).delete()
    VideoRating.query.filter_by(video_id=video_id).delete()
    from app.models import SkillCard
    SkillCard.query.filter_by(video_id=video_id).delete()
    db.session.delete(video)
    db.session.commit()
    return jsonify({'message': '视频已删除'})


@api.route('/videos/best', methods=['GET'])
def get_best_video():
    """
    问题图谱：获取某章节某题的最高分作业视频。
    参数: chapter, problem_no, subject（可选，默认材料力学）
    返回: {url, thumbnail, title, author} 或 {}
    """
    chapter    = request.args.get('chapter', type=int)
    problem_no = request.args.get('problem_no', type=int)
    subject    = request.args.get('subject', '材料力学')

    if not chapter or not problem_no:
        return jsonify({}), 400

    # 找评分最高的该题视频
    from sqlalchemy import func
    video = db.session.query(Video)\
        .outerjoin(VideoRating, VideoRating.video_id == Video.id)\
        .filter(
            Video.type == 'homework',
            Video.chapter == chapter,
            Video.problem_no == problem_no,
            Video.subject == subject,
        )\
        .group_by(Video.id)\
        .order_by(func.avg(VideoRating.value).desc().nullslast())\
        .first()

    if not video:
        return jsonify({})

    # 擂主信息
    from app.models import Champion
    champ = Champion.query.filter_by(problem_key=f'{chapter}-{problem_no}').first()

    return jsonify({
        'url':           video.url,
        'thumbnail':     video.thumbnail,
        'title':         video.title,
        'author':        video.author.username if video.author else '',
        'author_id':     video.author.id if video.author else None,
        'video_id':      video.id,
        'rating':        video.average_rating,
        'rating_count':  VideoRating.query.filter_by(video_id=video.id).count(),
        'comment_count': VideoComment.query.filter_by(video_id=video.id).count(),
        'champion': {
            'user_id':  champ.user_id,
            'username': champ.user.username,
        } if champ else None,
    })


@api.route('/videos/rankings', methods=['GET'])
def get_video_rankings():
    """
    问题图谱：某题全部作业视频按分数排名（rank 1 = 擂主/最高分）。
    参数: chapter, problem_no, subject（可选，默认材料力学）
    返回: [{rank, video_id, url, thumbnail, author, author_id, score}, ...] 分数降序
    """
    chapter    = request.args.get('chapter', type=int)
    problem_no = request.args.get('problem_no', type=int)
    subject    = request.args.get('subject', '材料力学')
    if not chapter or not problem_no:
        return jsonify([]), 400

    from sqlalchemy import func
    rows = db.session.query(
        Video,
        func.avg(VideoRating.value).label('avg_score')
    ).outerjoin(VideoRating, VideoRating.video_id == Video.id)\
     .filter(
         Video.type == 'homework',
         Video.chapter == chapter,
         Video.problem_no == problem_no,
         Video.subject == subject,
     )\
     .group_by(Video.id)\
     .order_by(func.avg(VideoRating.value).desc().nullslast())\
     .all()

    out = []
    for i, (v, avg) in enumerate(rows, 1):
        out.append({
            'rank':      i,
            'video_id':  v.id,
            'url':       v.url,
            'thumbnail': v.thumbnail,
            'author':    v.author.username if v.author else '',
            'author_id': v.author.id if v.author else None,
            'score':     round(float(avg), 1) if avg is not None else 0,
        })
    return jsonify(out)


@api.route('/videos/<int:video_id>/view', methods=['POST'])
@login_required
def track_view(video_id):
    """播放量计数：学生打开视频时调用，每次调用 views +1。"""
    video = Video.query.get_or_404(video_id)
    video.views = (video.views or 0) + 1
    db.session.commit()
    return jsonify({'views': video.views})


@api.route('/videos/<int:video_id>/compress_status', methods=['GET'])
@login_required
def compress_status(video_id):
    """查询视频压缩状态：若 .compressing.mp4 临时文件存在则表示正在压缩中。"""
    video = Video.query.get_or_404(video_id)
    # 从 URL 中提取文件名，URL 形如 /(.../)?static/uploads/<fname>
    parts = video.url.split('uploads/')
    if len(parts) < 2:
        return jsonify({'compressing': False, 'file_size_mb': None})
    fname = parts[-1].lstrip('/')
    upload_folder = _uf()
    file_path = os.path.join(upload_folder, fname)
    tmp_path = file_path + '.compressing.mp4'
    compressing = os.path.exists(tmp_path)
    size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 1) if os.path.exists(file_path) else None
    return jsonify({'compressing': compressing, 'file_size_mb': size_mb, 'video_id': video_id})