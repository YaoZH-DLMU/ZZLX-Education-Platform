import sys, os
sys.path.insert(0, '/app')
from app import create_app, db
from app.models import User, SignSession, SignResponse, PptSession, PptInteraction, PptResponse
from sqlalchemy import func

app = create_app()

with app.app_context():
    # ── 1. 学生积分 ──
    users = User.query.filter(User.is_teacher == False).order_by(
        User.reward_points.desc(), User.student_id
    ).all()
    real = [u for u in users if u.student_id and len(u.student_id) >= 6]

    print("=" * 55)
    print("学生积分（按积分降序，排除短学号测试账号）")
    print("=" * 55)
    print(f"{'学号':<14}{'姓名':<14}{'积分':>4}")
    print("-" * 35)
    has_pts = [u for u in real if (u.reward_points or 0) > 0]
    no_pts  = [u for u in real if not (u.reward_points or 0)]
    for u in has_pts:
        print(f"{u.student_id:<14}{u.username:<14}{u.reward_points:>4}")
    print(f"\n积分为 0 共 {len(no_pts)} 人（未参与任何课堂互动）")

    # ── 2. 最近一次签到：相同设备、不同学号 ──
    print("\n" + "=" * 55)
    print("最近签到会话 — 相同设备多学号检查")
    print("=" * 55)
    latest_sign = SignSession.query.order_by(SignSession.created_at.desc()).first()
    if latest_sign:
        print(f"会话：{latest_sign.question}  ({latest_sign.created_at.strftime('%Y-%m-%d %H:%M')})")
        resps = SignResponse.query.filter_by(session_id=latest_sign.id).all()
        # 按 ip+ua 分组
        device_map = {}
        for r in resps:
            key = (r.ip_addr or '', (r.ua_hint or '')[:80])
            device_map.setdefault(key, []).append(r.student_id)
        dup = {k: v for k, v in device_map.items() if len(v) > 1}
        if dup:
            print(f"发现 {len(dup)} 个设备有多个学号作答：")
            for (ip, ua), sids in dup.items():
                print(f"  IP={ip}  UA={ua[:60]}")
                print(f"  学号：{', '.join(sids)}")
        else:
            print("  未发现同设备多学号情况。")
    else:
        print("  数据库中暂无签到会话。")

    # ── 3. 最近一次 PPT 课堂：相同设备、不同学号 ──
    print("\n" + "=" * 55)
    print("最近 PPT 课堂会话 — 相同设备多学号检查")
    print("=" * 55)
    latest_ppt = PptSession.query.order_by(PptSession.created_at.desc()).first()
    if latest_ppt:
        print(f"课件：{latest_ppt.title}  ({latest_ppt.created_at.strftime('%Y-%m-%d %H:%M')})")
        inter_ids = [i.id for i in PptInteraction.query.filter_by(session_id=latest_ppt.id).all()]
        if inter_ids:
            resps2 = PptResponse.query.filter(PptResponse.interaction_id.in_(inter_ids)).all()
            device_map2 = {}
            for r in resps2:
                key = (r.ip_addr or '', (r.ua_hint or '')[:80])
                device_map2.setdefault(key, set()).add(r.student_id)
            dup2 = {k: list(v) for k, v in device_map2.items() if len(v) > 1}
            if dup2:
                print(f"发现 {len(dup2)} 个设备有多个学号作答：")
                for (ip, ua), sids in dup2.items():
                    print(f"  IP={ip}  UA={ua[:60]}")
                    print(f"  学号：{', '.join(sorted(sids))}")
            else:
                print("  未发现同设备多学号情况。")
        else:
            print("  该 PPT 课堂无互动记录。")
    else:
        print("  数据库中暂无 PPT 课堂会话。")
