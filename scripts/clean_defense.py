"""
清理数据库中 url 指向 /static/uploads/defense/ 的孤儿视频记录（文件已不存在）。
在容器内运行：
    docker exec zzlxweb-web-1 python scripts/clean_defense.py
"""
import sys
sys.path.insert(0, '/app')

from run import app
from app.models import Video, VideoComment, VideoFavorite, VideoRating, db

with app.app_context():
    vs = Video.query.filter(Video.url.like('/static/uploads/defense/%')).all()
    print('将删除 ' + str(len(vs)) + ' 条记录')
    for v in vs:
        print('  del id=' + str(v.id) + '  ' + v.url)
        VideoComment.query.filter_by(video_id=v.id).delete()
        VideoFavorite.query.filter_by(video_id=v.id).delete()
        VideoRating.query.filter_by(video_id=v.id).delete()
        db.session.delete(v)
    db.session.commit()
    print('清理完成')
