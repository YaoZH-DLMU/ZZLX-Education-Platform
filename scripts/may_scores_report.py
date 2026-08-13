"""
5月积分变化报告（只读，不修改数据库）
======================================
用途：按新积分规则重新计算2025-05-01以来的 interact_join 积分
      （原来每次互动参与 +1.0，新规则改为 +0.5），输出差异报告。

新规则说明：
  - interact_join: 从 +1.0 改为 +0.5（每次互动参与基础分减半）
  - interact_correct: 新增 +1.0（答对题目），但历史数据中没有正确答案信息，无法计算
  - interact_rank_*: 不变

注意：correct_answers 没有历史数据，报告仅计算参与分差异。

运行方式（在 Docker 容器内）：
    docker exec zzlxweb-web-1 python scripts/may_scores_report.py

或在宿主机（有数据库访问）：
    cd /opt/ZZLXWeb && python scripts/may_scores_report.py
"""

import sys
import os
import csv
from io import StringIO
from datetime import datetime

# 允许直接运行（不通过 Flask 上下文）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

try:
    from app import create_app, db
    from app.models import User, PointLog

    app = create_app()
    ctx = app.app_context()
    ctx.push()
    HAVE_APP = True
except Exception as e:
    print(f"[WARN] 无法加载 Flask app，尝试直接连接数据库: {e}")
    HAVE_APP = False

CUTOFF_DATE = datetime(2025, 5, 1, 0, 0, 0)

# interact_join 旧积分值 → 新积分值
OLD_JOIN_PTS = 1.0
NEW_JOIN_PTS = 0.5
DELTA_PER_JOIN = NEW_JOIN_PTS - OLD_JOIN_PTS   # -0.5

def run_report():
    if not HAVE_APP:
        print("错误：无法创建 Flask 应用上下文，请检查配置。")
        sys.exit(1)

    # ── 统计正式学生用户 ───────────────────────────────────
    # 学号 < 10 位为测试账号，不计入报告
    users = User.query.filter(
        User.is_teacher == False,
        db.func.length(User.student_id) >= 10
    ).all()

    print(f"找到 {len(users)} 个正式学生账号（学号>=10位）")

    report_rows = []
    total_delta = 0.0
    affected = 0

    for user in users:
        # 2025-05-01 以来该用户的 interact_join 次数
        join_logs = PointLog.query.filter(
            PointLog.user_id == user.id,
            PointLog.reason == 'interact_join',
            PointLog.created_at >= CUTOFF_DATE
        ).all()

        n_joins = len(join_logs)
        if n_joins == 0:
            continue

        # 旧总分（已发放）= n_joins * 1.0
        old_join_pts = sum(float(l.points) for l in join_logs)
        # 新总分 = n_joins * 0.5
        new_join_pts = n_joins * NEW_JOIN_PTS
        delta = new_join_pts - old_join_pts   # 通常为负数（-0.5 * n_joins）

        current_total = float(user.reward_points or 0)
        new_total = current_total + delta

        total_delta += delta
        affected += 1

        report_rows.append({
            '学号':         user.student_id,
            '姓名':         user.username,
            '当前积分':     round(current_total, 2),
            '5月互动参与次数': n_joins,
            '旧参与积分合计': round(old_join_pts, 2),
            '新参与积分合计': round(new_join_pts, 2),
            '积分变化':     round(delta, 2),
            '调整后积分':   round(new_total, 2),
        })

    # ── 排序：按积分变化从大到小（绝对值）────────────────
    report_rows.sort(key=lambda r: r['积分变化'])

    # ── 输出报告 ──────────────────────────────────────────
    now_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(BASE_DIR, 'exports')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'may_scores_report_{now_str}.csv')

    fields = ['学号','姓名','当前积分','5月互动参与次数','旧参与积分合计','新参与积分合计','积分变化','调整后积分']
    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report_rows)

    # ── 汇总打印 ──────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  5月积分变化报告（新规则：参与 +0.5，旧规则：参与 +1.0）")
    print(f"  统计范围：2025-05-01 至今")
    print(f"{'='*60}")
    print(f"  受影响学生人数：{affected}")
    print(f"  积分总变化量：  {round(total_delta, 2)}")
    print(f"  报告文件：      {out_path}")
    print(f"{'='*60}")

    if report_rows:
        print(f"\n{'学号':<14} {'姓名':<10} {'当前积分':>8} {'参与次数':>8} {'积分变化':>8} {'调整后积分':>10}")
        print('-' * 65)
        for r in report_rows[:30]:  # 仅打印前30行
            print(f"{r['学号']:<14} {r['姓名']:<10} {r['当前积分']:>8.1f} "
                  f"{r['5月互动参与次数']:>8} {r['积分变化']:>+8.1f} {r['调整后积分']:>10.1f}")
        if len(report_rows) > 30:
            print(f"  ... 还有 {len(report_rows)-30} 条，请查看 CSV 文件")

    print(f"\n[提示] 确认无误后，可运行 scripts/apply_may_scores.py 将变化写入数据库")
    return report_rows


if __name__ == '__main__':
    run_report()
    if HAVE_APP:
        ctx.pop()
