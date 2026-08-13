"""
重置金银铜牌积分：退还所有历史错误奖励，清除结算记录，以便重新结算。

用法（在容器内执行）：
  docker exec zzlxweb-web-1 python /app/scripts/reset_medals.py
"""
import sys, os
sys.path.insert(0, '/app')

from app import create_app
from app.models import db, PointLog, PointSettlement, User

app = create_app()

MEDAL_TYPES = ('medal_gold', 'medal_silver', 'medal_bronze')

with app.app_context():
    # 1. 查出所有金银铜牌积分日志
    logs = PointLog.query.filter(PointLog.reason.in_(MEDAL_TYPES)).all()

    if not logs:
        print("✅ 数据库中没有金银铜牌积分记录，无需处理。")
        sys.exit(0)

    print(f"找到 {len(logs)} 条金银铜牌积分记录，准备退还：")

    # 2. 逐条退还积分
    for log in logs:
        user = User.query.get(log.user_id)
        if user:
            before = user.reward_points or 0
            user.reward_points = max(0, before - log.points)
            print(f"  [{log.reason}] {user.username}  -{log.points}  "
                  f"({before} → {user.reward_points})  memo: {log.memo}")
        db.session.delete(log)

    # 3. 清除积分结算去重记录（允许重新结算）
    settled = PointSettlement.query.filter(
        PointSettlement.award_type.in_(MEDAL_TYPES)
    ).all()
    print(f"\n清除 {len(settled)} 条结算去重记录。")
    for s in settled:
        db.session.delete(s)

    db.session.commit()
    print("\n✅ 完成！现在可以在 course_mgmt 页面重新点击「积分结算」。")
