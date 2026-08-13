"""
Phase 7: 图谱视频 API
- GET  /api/graph/videos          获取所有已上传节点的视频信息（{node_id: {url, thumbnail}}）
- POST /api/graph/videos/<node_id> 教师上传/替换某 level2 节点的慕课视频
"""
import os, time, subprocess
from flask import request, jsonify, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from . import api
from app import db
from app.models import GraphVideo


def _allowed(filename):
    exts = {'mp4', 'webm', 'mov', 'avi', 'mkv'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in exts


@api.route('/graph/videos', methods=['GET'])
@login_required
def get_graph_videos():
    """返回所有已绑定节点的视频映射 {node_id: {url, thumbnail}}"""
    rows = GraphVideo.query.all()
    data = {
        r.node_id: {
            'url': r.url,
            'thumbnail': r.thumbnail,
            'uploaded_by': r.uploader.username if r.uploader else None,
        }
        for r in rows
    }
    return jsonify(data)


@api.route('/graph/videos/<path:node_id>', methods=['POST'])
@login_required
def upload_graph_video(node_id):
    """教师上传/替换 level2 节点慕课视频"""
    if not current_user.is_teacher:
        return jsonify({'message': '仅教师可上传图谱视频'}), 403

    if 'video' not in request.files:
        return jsonify({'message': '未包含视频文件'}), 400

    f = request.files['video']
    if not f or not _allowed(f.filename):
        return jsonify({'message': '不支持的文件类型，请上传 mp4/webm/mov'}), 400

    from app.utils.course_paths import upload_folder as _uf
    upload_dir = os.path.join(_uf(), 'graph')
    os.makedirs(upload_dir, exist_ok=True)

    safe_node = secure_filename(node_id)
    filename = f"{safe_node}_{int(time.time())}_{secure_filename(f.filename)}"
    video_path = os.path.join(upload_dir, filename)
    f.save(video_path)

    # 生成缩略图
    thumbnail_url = None
    try:
        thumb_dir = os.path.join(upload_dir, 'thumbnails')
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_name = filename.rsplit('.', 1)[0] + '.jpg'
        thumb_path = os.path.join(thumb_dir, thumb_name)
        subprocess.run(
            ['ffmpeg', '-i', video_path, '-ss', '0.5', '-vframes', '1',
             '-vf', 'scale=480:-1', '-q:v', '5', '-y', thumb_path],
            capture_output=True, timeout=30
        )
        if os.path.exists(thumb_path):
            thumbnail_url = f'/static/uploads/graph/thumbnails/{thumb_name}'
    except Exception:
        pass

    video_url = f'/static/uploads/graph/{filename}'

    # 更新或新建记录
    gv = GraphVideo.query.filter_by(node_id=node_id).first()
    if gv:
        gv.url = video_url
        gv.thumbnail = thumbnail_url
        gv.uploaded_by = current_user.id
    else:
        gv = GraphVideo(
            node_id=node_id,
            url=video_url,
            thumbnail=thumbnail_url,
            uploaded_by=current_user.id,
        )
        db.session.add(gv)

    db.session.commit()
    return jsonify({'url': video_url, 'thumbnail': thumbnail_url, 'node_id': node_id})
