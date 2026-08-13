"""
修复 closest_score 积分虚高：每人只保留最早的3条，多余的退还。

用法：
  docker exec zzlxweb-web-1 python /app/scripts/fix_closest_score.py
加 --dry-run 仅预览不写库：
  docker exec zzlxweb-web-1 python /app/scripts/fix_closest_score.py --dry-run
"""
import sys
sys.path.insert(0, '/app')

dry_run = '--dry-run' in sys.argv

from app import create_app
from app.models import db, PointLog, User
from collections import defaultdict

KEEP = 3   # 每人保留条数

app = create_app()
with app.app_context():
    # 按用户分组查出所有 closest_score 日志（按 id 升序 = 时间早的在前）
    logs = PointLog.query.filter_by(reason='closest_score')\
        .order_by(PointLog.id.asc()).all()

    by_user = defaultdict(list)
    for l in logs:
        by_user[l.user_id].append(l)

    total_refund = 0
    affected = 0

    for uid, user_logs in by_user.items():
        excess = user_logs[KEEP:]   # 第 KEEP+1 条起全部删除
        if not excess:
            continue

        user = User.query.get(uid)
        refund = sum(l.points for l in excess)
        before = user.reward_points or 0
        after  = max(0, before - refund)

        print(f"  {user.username:<12} 共{len(user_logs)}条 → 保留{KEEP}条 "
              f"退还{refund:.1f}分 ({before} → {after})")

        if not dry_run:
            user.reward_points = after
            for l in excess:
                db.session.delete(l)

        total_refund += refund
        affected += 1

    if affected == 0:
        print("✅ 所有用户 closest_score 条数均 ≤ 3，无需处理。")
    else:
        print(f"\n共 {affected} 人需要调整，合计退还 {total_refund:.1f} 分。")
        if not dry_run:
            db.session.commit()
            print("✅ 已写入数据库。")
        else:
            print("[DRY RUN] 未写入数据库。")
