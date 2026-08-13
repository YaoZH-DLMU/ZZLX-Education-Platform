"""
app/api/champion_api.py
打擂台系统 API
===========================================

POST /api/champion/declare
  Body: { "video_id": 123, "problem_key": "3-7" }
  守擂：当前最高分作者宣布成为该题擂主
  条件：
    - 调用者是 video_id 的作者
    - 该视频是该 problem_key 当前最高分视频
    - 评分人数 ≥ 5 且评论数 ≥ 3
    - 用户当前同时持有的擂主席位 < 5

GET  /api/champion/info?problem_key=3-7
  返回当前该题目的擂主信息（无擂主返回 null）
"""

from flask import request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from . import api
from app import db
from app.models import Champion, Video, VideoRating, VideoComment

# 门槛常量（可按需调整）
THRESHOLD_RATERS   = 5   # 最少评分人数
THRESHOLD_COMMENTS = 3   # 最少评论数
MAX_CHAMPION_SLOTS = 5   # 用户同时持有的最多席位数


@api.route('/champion/info', methods=['GET'])
@login_required
def get_champion_info():
    """查询某题目当前擂主信息。"""
    problem_key = request.args.get('problem_key', '').strip()
    if not problem_key:
        return jsonify({'error': '缺少 problem_key'}), 400

    champ = Champion.query.filter_by(problem_key=problem_key).first()
    if not champ:
        return jsonify({'champion': None})

    return jsonify({
        'champion': {
            'user_id':     champ.user_id,
            'username':    champ.user.username,
            'video_id':    champ.video_id,
            'declared_at': champ.declared_at.strftime('%Y-%m-%d'),
        }
    })


@api.route('/champion/declare', methods=['POST'])
@login_required
def declare_champion():
    """守擂：宣布成为该题擂主。"""
    if current_user.is_teacher:
        return jsonify({'error': '教师账号不参与打擂台'}), 403

    data = request.get_json(silent=True) or {}
    video_id    = data.get('video_id')
    problem_key = str(data.get('problem_key', '')).strip()

    if not video_id or not problem_key:
        return jsonify({'error': '缺少 video_id 或 problem_key'}), 400

    video = Video.query.get(video_id)
    if not video:
        return jsonify({'error': '视频不存在'}), 404
    if video.user_id != current_user.id:
        return jsonify({'error': '只有视频作者才能守擂'}), 403

    # ── 门槛检查 ──
    rating_count  = VideoRating.query.filter_by(video_id=video_id).count()
    comment_count = VideoComment.query.filter_by(video_id=video_id).count()
    if rating_count < THRESHOLD_RATERS:
        return jsonify({'error': f'视频需至少 {THRESHOLD_RATERS} 人评分才能守擂（当前 {rating_count} 人）'}), 400
    if comment_count < THRESHOLD_COMMENTS:
        return jsonify({'error': f'视频需至少 {THRESHOLD_COMMENTS} 条评论才能守擂（当前 {comment_count} 条）'}), 400

    # ── 确认该视频是该题最高分 ──
    parts = problem_key.split('-')
    if len(parts) != 2:
        return jsonify({'error': 'problem_key 格式应为 "章-题"，如 "3-7"'}), 400
    chap, prob = int(parts[0]), int(parts[1])

    best = db.session.query(Video)\
        .outerjoin(VideoRating, VideoRating.video_id == Video.id)\
        .filter(
            Video.type == 'homework',
            Video.chapter == chap,
            Video.problem_no == prob,
        )\
        .group_by(Video.id)\
        .order_by(func.avg(VideoRating.value).desc().nullslast())\
        .first()

    if not best or best.id != video_id:
        return jsonify({'error': '只有该题当前最高分视频的作者才能守擂'}), 400

    # ── 席位检查（此 problem_key 本身不算新增）──
    existing = Champion.query.filter_by(problem_key=problem_key).first()
    if existing and existing.user_id == current_user.id:
        # 已经是这道题的擂主，更新 video_id 即可
        existing.video_id   = video_id
        existing.declared_at = db.func.now()
        db.session.commit()
        return jsonify({'success': True, 'message': '守擂成功（更新）'})

    current_slots = Champion.query.filter_by(user_id=current_user.id).count()
    if current_slots >= MAX_CHAMPION_SLOTS:
        return jsonify({'error': f'你已同时持有 {MAX_CHAMPION_SLOTS} 个擂主席位，无法再加'}), 400

    # ── 写入 / 更新擂主记录 ──
    if existing:
        # 前任擂主被替换，更新记录
        old_user_id   = existing.user_id
        existing.user_id    = current_user.id
        existing.video_id   = video_id
        existing.declared_at = db.func.now()
    else:
        existing = Champion(
            problem_key = problem_key,
            user_id     = current_user.id,
            video_id    = video_id,
        )
        db.session.add(existing)

    db.session.commit()
    return jsonify({'success': True, 'message': '守擂成功！你已成为该题擂主 👑'})
