"""
通知系统 API
GET  /api/notifications          - 获取当前用户的通知列表（分页）
POST /api/notifications/<id>/read - 将单条通知标记为已读
POST /api/notifications/read_all  - 将所有未读通知标记为已读
GET  /api/notifications/unread_count - 获取未读通知数量（供导航栏轮询）
DELETE /api/notifications/<id>    - 删除单条通知
"""
from flask import jsonify, request
from flask_login import current_user, login_required
from . import api
from ..models import Notification, db


@api.route('/notifications')
@login_required
def get_notifications():
    """获取当前用户的通知列表，支持 ?page=1&per_page=20&unread_only=false"""
    page        = request.args.get('page', 1, type=int)
    per_page    = request.args.get('per_page', 20, type=int)
    unread_only = request.args.get('unread_only', 'false').lower() == 'true'

    q = Notification.query.filter_by(user_id=current_user.id)
    if unread_only:
        q = q.filter_by(is_read=False)
    q = q.order_by(Notification.created_at.desc())

    paginated = q.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'notifications': [n.to_dict() for n in paginated.items],
        'total':         paginated.total,
        'pages':         paginated.pages,
        'current_page':  paginated.page,
        'unread_count':  Notification.query.filter_by(
                             user_id=current_user.id, is_read=False).count(),
    })


@api.route('/notifications/unread_count')
@login_required
def unread_count():
    """返回未读通知数量，供导航栏定时轮询"""
    count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False).count()
    return jsonify({'unread_count': count})


@api.route('/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_read(notif_id):
    """将指定通知标记为已读"""
    notif = Notification.query.filter_by(
        id=notif_id, user_id=current_user.id).first_or_404()
    notif.is_read = True
    db.session.commit()
    return jsonify({'success': True})


@api.route('/notifications/read_all', methods=['POST'])
@login_required
def mark_all_read():
    """将当前用户的所有未读通知标记为已读"""
    Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


@api.route('/notifications/<int:notif_id>', methods=['DELETE'])
@login_required
def delete_notification(notif_id):
    """删除单条通知"""
    notif = Notification.query.filter_by(
        id=notif_id, user_id=current_user.id).first_or_404()
    db.session.delete(notif)
    db.session.commit()
    return jsonify({'success': True})
