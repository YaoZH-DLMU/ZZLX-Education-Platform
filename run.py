from app import create_app, db
from app.models import User, Video, VideoComment, VideoFavorite, VideoRating
from app.models import ForumBoard, Post, Reply, PostImage, ReplyImage
from app.models import Notification
from config import Config
from werkzeug.security import generate_password_hash
from sqlalchemy import text as _text
import os

app = create_app()

with app.app_context():
    app.config.from_object('config.Config')
    Config.init_app(app)

    from app.courses import COURSES

    # 每门课的教师 + 助教账号（学号 202601-202610 为助教，可浏览教师页但不能写入）
    TEACHERS = [
        ('管理员', '1000001', 'change-me'),    # 管理员（部署前改成你自己的）
    ]
    TAS = [
        (f'助教{i:02d}', f'2026{i:02d}', 'ta@2026')  # 助教01-10, 学号202601-202610
        for i in range(1, 11)
    ]

    # 在所有课程库中建表 + 播种
    for key in COURSES:
        print(f'[{key}] 初始化数据库...', flush=True)
        # Flask-SQLAlchemy 3.x: create_all(bind_key=...) 只建有 __bind_key__ 的模型
        # 我们的模型没有 __bind_key__（都在默认 metadata），需用 metadata.create_all(engine)
        engine = db.engines[key]
        db.metadata.create_all(engine)

        with engine.begin() as conn:
            # 检查是否已有教师
            row = conn.execute(_text("SELECT count(*) FROM user WHERE is_teacher = 1")).scalar()
            if row == 0:
                for t_name, t_id, t_pwd in TEACHERS + TAS:
                    pwd_hash = generate_password_hash(t_pwd)
                    conn.execute(_text(
                        "INSERT OR IGNORE INTO user (username, student_id, password_hash, is_teacher, created_at) "
                        "VALUES (:name, :sid, :pwd, 1, datetime('now'))"
                    ), {'name': t_name, 'sid': t_id, 'pwd': pwd_hash})
                print(f'  [{key}] 播种 {len(TEACHERS)} 教师 + {len(TAS)} 助教', flush=True)
            else:
                print(f'  [{key}] 已有教师，跳过播种', flush=True)

            # 检查论坛板块
            row = conn.execute(_text("SELECT count(*) FROM forum_boards")).scalar()
            if row == 0:
                course = COURSES[key]
                for name, desc in course.get('forum_boards', []):
                    conn.execute(_text(
                        "INSERT INTO forum_boards (name, description, created_at) "
                        "VALUES (:name, :desc, datetime('now'))"
                    ), {'name': name, 'desc': desc})
                print(f'  [{key}] 播种论坛板块', flush=True)

        # 创建上传子目录（非 zzlx 课程）
        if key != 'zzlx':
            upload_sub = os.path.join(app.config['UPLOAD_FOLDER'], key)
            os.makedirs(upload_sub, exist_ok=True)
            for subdir in ['avatars', 'forum', 'ppt', 'graph', 'thumbnails']:
                os.makedirs(os.path.join(upload_sub, subdir), exist_ok=True)
            print(f'  [{key}] 创建上传目录', flush=True)

    print('全部课程库初始化完成', flush=True)

if __name__ == '__main__':
    app.run(debug=True)
