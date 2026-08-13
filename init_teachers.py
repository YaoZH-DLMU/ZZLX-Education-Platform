"""初始化教师账号（幂等：已存在则更新，不会重复创建）
部署后在服务器运行：docker compose exec web python init_teachers.py
请把下方 TEACHERS 改成你自己的管理员账号。
"""
from app import create_app, db
from app.models import User

TEACHERS = [
    {'username': '管理员', 'student_id': '1000001', 'password': 'change-me'},
]

app = create_app()
with app.app_context():
    for t in TEACHERS:
        existing = User.query.filter_by(student_id=t['student_id']).first()
        if not existing:
            existing = User.query.filter_by(username=t['username']).first()
            if existing and existing.student_id != t['student_id']:
                existing.student_id = t['student_id']
        if existing:
            existing.is_teacher = True
            existing.username   = t['username']
            existing.student_id = t['student_id']
            existing.set_password(t['password'])
            db.session.commit()
            print(f'  已更新: {t["username"]} (学号 {t["student_id"]})')
        else:
            user = User(username=t['username'], student_id=t['student_id'], is_teacher=True)
            user.set_password(t['password'])
            db.session.add(user)
            db.session.commit()
            print(f'  已创建: {t["username"]} (学号 {t["student_id"]})')

    print('\n教师账号初始化完成。')
