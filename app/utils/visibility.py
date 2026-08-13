"""
app/utils/visibility.py
内容可见性过滤（测试账号隐藏）共享逻辑
========================================
原 routes.py / video_api.py / forum_api.py 各有一份同名副本，现统一至此。
"""

from sqlalchemy import or_
from flask_login import current_user

from app import db
from app.models import User

# 学号 1-50 为测试账号，其内容仅本人与教师可见
TEST_STUDENT_IDS = [str(i) for i in range(1, 51)]


def hide_test_content(user_id_col):
    """
    返回过滤条件：测试账号(学号1-50)的内容对非本人且非教师用户隐藏。
    返回 None 表示不需要过滤（教师看全部）。
    """
    if current_user.is_authenticated and current_user.is_teacher:
        return None  # 教师看全部
    test_q = db.session.query(User.id).filter(User.student_id.in_(TEST_STUDENT_IDS))
    if current_user.is_authenticated:
        return or_(user_id_col.notin_(test_q), user_id_col == current_user.id)
    return user_id_col.notin_(test_q)
