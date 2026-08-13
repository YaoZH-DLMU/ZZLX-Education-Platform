"""
app/utils/video.py
视频文件处理工具（上传流程共享逻辑）
========================================
"""

import os
import subprocess

from flask import url_for


def generate_thumbnail(video_path, source_filename, upload_folder):
    """
    用 ffmpeg 截取视频第 0.5 秒、缩放至 480px 宽，生成 jpg 缩略图。

    返回缩略图的 url_for 路径（如 /static/uploads/thumbnails/xxx.jpg）；
    ffmpeg 不可用或截帧失败时返回 None，不阻断上传流程。

    参数：
      video_path      已保存的视频文件绝对路径
      source_filename 视频文件名（取主名作为缩略图文件名）
      upload_folder   上传根目录（缩略图存放于其下 thumbnails/ 子目录）
    """
    try:
        thumb_dir = os.path.join(upload_folder, 'thumbnails')
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_name = source_filename.rsplit('.', 1)[0] + '.jpg'
        thumb_path = os.path.join(thumb_dir, thumb_name)
        subprocess.run(
            ['ffmpeg', '-i', video_path, '-ss', '0.5', '-vframes', '1',
             '-vf', 'scale=480:-1', '-q:v', '5', '-y', thumb_path],
            capture_output=True, timeout=30
        )
        if os.path.exists(thumb_path):
            return url_for('static', filename=f'uploads/thumbnails/{thumb_name}')
    except Exception:
        pass
    return None
