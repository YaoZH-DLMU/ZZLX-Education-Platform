"""
创建高级测试账号 202601~202610
=====================================
在服务器上运行一次即可：
    docker exec zzlxweb-web-1 python scripts/create_adv_test_accounts.py

账号信息：
  学号：202601 ~ 202610
  密码：zzlx@test01 ~ zzlx@test10
  权限：普通学生（is_teacher=False），学号6位长度，属于测试账号
  功能：可访问教师页面（词语签到、课堂PPT、课程管理），但禁止所有写入操作
"""

import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from app import create_app, db
from app.models import User

def create_accounts():
    app = create_app()
    with app.app_context():
        created = []
        skipped = []

        for i in range(1, 11):
            sid  = f'20260{i}'   # 202601 ~ 202610
            pwd  = f'zzlx@test{i:02d}'
            name = f'测试观察员{i:02d}'

            if User.query.filter_by(student_id=sid).first():
                skipped.append(sid)
                continue

            user = User(student_id=sid, username=name, is_teacher=False)
            user.set_password(pwd)
            db.session.add(user)
            created.append((sid, pwd, name))

        db.session.commit()

        print(f"{'='*55}")
        print(f"  高级测试账号创建完成")
        print(f"{'='*55}")
        if created:
            print(f"  新建 {len(created)} 个账号：")
            for sid, pwd, name in created:
                print(f"    学号: {sid}  密码: {pwd}  姓名: {name}")
        if skipped:
            print(f"  已跳过（已存在）：{', '.join(skipped)}")
        print(f"{'='*55}")
        print(f"  这些账号可访问教师功能页面（只读），但无法修改、删除或下载任何数据。")

if __name__ == '__main__':
    create_accounts()
