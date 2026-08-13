import sys, os
sys.path.insert(0, '/app')
os.chdir('/app')
from app import create_app, db
from app.models import User
app = create_app()
ctx = app.app_context()
ctx.push()

# 查测试账号（学号1-50）
ids = [str(i) for i in range(1, 51)]
rows = User.query.filter(User.student_id.in_(ids)).all()
print(f"Test accounts found: {len(rows)}")
for u in rows[:5]:
    print(f"  id={u.id}  student_id={repr(u.student_id)}  username={u.username}")

# 再查全部用户前5条
all5 = User.query.order_by(User.id).limit(5).all()
print("First 5 users in DB:")
for u in all5:
    print(f"  id={u.id}  student_id={repr(u.student_id)}  username={u.username}")
