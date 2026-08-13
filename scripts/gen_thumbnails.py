"""
为已上传的历史视频批量生成缩略图，并更新数据库中的 thumbnail 字段。
在容器内运行：
    docker exec zzlxweb-web-1 python scripts/gen_thumbnails.py
"""
import os
import subprocess
import sys

# 确保能 import Flask app
sys.path.insert(0, '/app')

from run import app
from app.models import Video
from app import db
from flask import url_for

UPLOAD_DIR   = '/app/app/static/uploads'
THUMB_DIR    = os.path.join(UPLOAD_DIR, 'thumbnails')
os.makedirs(THUMB_DIR, exist_ok=True)

with app.app_context():
    videos = Video.query.filter(Video.thumbnail == None).all()
    print(f"需要补缩略图的视频数量：{len(videos)}")

    ok = 0
    for v in videos:
        # url 格式为 /static/uploads/xxx.mp4（含 /ZZLX 前缀时 strip 掉）
        url_path = v.url
        # 取 uploads/ 后面的部分作为文件名
        if '/uploads/' not in url_path:
            continue
        rel = url_path.split('/uploads/', 1)[1]   # e.g. "1_1740037208_xxx.mp4"
        video_path = os.path.join(UPLOAD_DIR, rel)
        if not os.path.exists(video_path):
            print(f"  [skip] 文件不存在: {video_path}")
            continue

        thumb_name = rel.rsplit('.', 1)[0] + '.jpg'
        thumb_path = os.path.join(THUMB_DIR, thumb_name)

        if os.path.exists(thumb_path):
            # 文件已存在，只更新 DB 字段
            pass
        else:
            result = subprocess.run(
                ['ffmpeg', '-i', video_path, '-ss', '0.5', '-vframes', '1',
                 '-vf', 'scale=480:-1', '-q:v', '5', '-y', thumb_path],
                capture_output=True, timeout=60
            )
            if result.returncode != 0 or not os.path.exists(thumb_path):
                print(f"  [fail] {rel}: {result.stderr[-200:].decode(errors='ignore')}")
                continue

        with app.test_request_context('/'):
            thumb_url = url_for('static', filename=f'uploads/thumbnails/{thumb_name}')
        v.thumbnail = thumb_url
        ok += 1
        print(f"  [ok] {rel}")

    db.session.commit()
    print(f"\n完成，成功处理 {ok}/{len(videos)} 个视频")
