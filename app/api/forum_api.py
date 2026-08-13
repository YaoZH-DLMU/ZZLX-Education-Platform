from flask import jsonify, request, current_app
from flask_login import current_user, login_required
from sqlalchemy import or_
from . import api
from ..models import ForumBoard, Post, Reply, PostImage, ReplyImage, PostLike, User, db
from ..utils.ai_client import async_classify
from ..utils.visibility import hide_test_content as _hide_test_content


@api.route('/forum/boards', methods=['GET'])
def get_boards():
    """获取所有论坛板块"""
    boards = ForumBoard.query.all()
    return jsonify({
        'boards': [{
            'id': b.id,
            'name': b.name,
            'description': b.description,
            'post_count': b.posts.count()
        } for b in boards]
    })


@api.route('/forum/posts', methods=['GET'])
def get_posts():
    """获取帖子列表，支持按板块过滤和分页"""
    board_id = request.args.get('board_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    query = Post.query
    if board_id:
        query = query.filter_by(board_id=board_id)
    f = _hide_test_content(Post.author_id)
    if f is not None:
        query = query.filter(f)
    query = query.order_by(Post.created_at.desc())
    posts = query.paginate(page=page, per_page=per_page)

    return jsonify({
        'posts': [{
            'id': p.id,
            'title': p.title,
            'content': p.content,
            'author': p.author.username,
            'board_id': p.board_id,
            'created_at': p.created_at.isoformat(),
            'reply_count': p.replies.count(),
            'views': p.views
        } for p in posts.items],
        'total': posts.total,
        'pages': posts.pages,
        'current_page': posts.page
    })


@api.route('/forum/posts/<int:post_id>/replies', methods=['GET', 'POST'])
@login_required
def post_replies(post_id):
    """获取或新增帖子回复"""
    post = Post.query.get_or_404(post_id)

    if request.method == 'POST':
        content = (request.json or {}).get('content') or request.form.get('content')
        if not content:
            return jsonify({'message': '回复内容不能为空'}), 400
        reply = Reply(content=content, post_id=post_id, author_id=current_user.id)
        db.session.add(reply)
        db.session.commit()
        # 后台异步判断回复是否有意义
        async_classify(current_app._get_current_object(),
                       'Reply', reply.id, content)
        return jsonify({
            'message': '回复成功',
            'reply': {
                'id': reply.id,
                'author': current_user.username,
                'content': reply.content,
                'created_at': reply.created_at.isoformat()
            }
        })

    replies = Reply.query.filter_by(post_id=post_id).order_by(Reply.created_at.asc())
    rf = _hide_test_content(Reply.author_id)
    if rf is not None:
        replies = replies.filter(rf)
    return jsonify([{
        'id': r.id,
        'content': r.content,
        'author': r.author.username,
        'created_at': r.created_at.isoformat()
    } for r in replies])


@api.route('/forum/posts/<int:post_id>', methods=['DELETE'])
@login_required
def delete_post(post_id):
    """教师账号删除帖子（含全部回复及图片）"""
    if not current_user.is_teacher:
        return jsonify({'message': '无权限，仅教师可删除帖子'}), 403
    post = Post.query.get_or_404(post_id)
    for reply in post.replies.all():
        ReplyImage.query.filter_by(reply_id=reply.id).delete()
        db.session.delete(reply)
    PostImage.query.filter_by(post_id=post_id).delete()
    PostLike.query.filter_by(post_id=post_id).delete()
    db.session.delete(post)
    db.session.commit()
    return jsonify({'message': '帖子已删除'})


@api.route('/forum/posts/<int:post_id>/like', methods=['POST'])
@login_required
def toggle_post_like(post_id):
    """点赞/取消点赞帖子"""
    post = Post.query.get_or_404(post_id)
    existing = PostLike.query.filter_by(user_id=current_user.id, post_id=post_id).first()
    if existing:
        db.session.delete(existing)
        post.like_count = max(0, (post.like_count or 0) - 1)
        db.session.commit()
        return jsonify({'liked': False, 'like_count': post.like_count})
    else:
        like = PostLike(user_id=current_user.id, post_id=post_id)
        db.session.add(like)
        post.like_count = (post.like_count or 0) + 1
        db.session.commit()
        return jsonify({'liked': True, 'like_count': post.like_count})
 