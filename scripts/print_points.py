"""输出积分明细：金银铜牌结算记录 + 全体学生积分排名"""
import sys
sys.path.insert(0, '/app')
from app import create_app
from app.models import PointLog, PointSettlement, User, Video, db

app = create_app()
with app.app_context():
    print('=== 金银铜牌结算记录 ===')
    medals = PointSettlement.query.filter(
        PointSettlement.award_type.in_(['medal_gold','medal_silver','medal_bronze'])
    ).all()
    for s in medals:
        log = PointLog.query.filter_by(ref_video_id=s.video_id, reason=s.award_type).first()
        u = User.query.get(log.user_id) if log else None
        print(f'  {s.award_type:<14} video_id={s.video_id}  获奖者:{u.username if u else "?"}  +{log.points if log else "?"}  {log.memo if log else ""}')

    print()
    print('=== 学生积分排名 ===')
    students = User.query.filter_by(is_teacher=False)\
        .order_by(User.reward_points.desc()).all()
    for i, u in enumerate(students, 1):
        pts = u.reward_points or 0
        # 该学生的积分明细
        logs = PointLog.query.filter_by(user_id=u.id).order_by(PointLog.id).all()
        detail = ', '.join(f'{l.reason}(+{l.points})' for l in logs) if logs else '无'
        print(f'  {i:3d}. {u.username:<12} {u.student_id}  总积分:{pts:6.1f}  [{detail}]')
