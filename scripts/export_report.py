"""
export_report.py — 导出学生积分 + 视频播放量报告
用法：docker exec zzlxweb-web-1 python /app/scripts/export_report.py
输出：/app/exports/report_YYYYMMDD.txt
"""
import sys, os, csv
from datetime import datetime

sys.path.insert(0, '/app')
os.environ.setdefault('FLASK_ENV', 'production')

from app import create_app, db
from app.models import User, Video, PointLog

app = create_app()

OUTDIR = '/app/exports'
os.makedirs(OUTDIR, exist_ok=True)
stamp = datetime.now().strftime('%Y%m%d_%H%M')
out_txt = os.path.join(OUTDIR, f'report_{stamp}.txt')
out_csv = os.path.join(OUTDIR, f'report_{stamp}.csv')

with app.app_context():
    # ── 学生积分表 ──────────────────────────────────────────────────
    students = (User.query
                .filter(User.is_teacher == False, User.student_id.isnot(None))
                .filter(db.func.length(User.student_id) >= 10)   # 排除短学号测试账号
                .order_by(User.reward_points.desc().nullslast(), User.student_id)
                .all())

    # ── 各视频统计 ────────────────────────────────────────────────
    videos = (Video.query
              .filter(db.or_(Video.type == 'homework', Video.type == None))
              .order_by(Video.chapter.asc().nullslast(),
                        Video.problem_no.asc().nullslast(),
                        Video.created_at.asc())
              .all())

    # ─── 输出文本报告 ─────────────────────────────────────────────
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write(f"===== 积分 & 播放量报告  生成时间：{datetime.now():%Y-%m-%d %H:%M} =====\n\n")

        # 学生积分
        f.write("【学生积分排行】\n")
        f.write(f"{'排名':<5} {'学号':<16} {'姓名':<12} {'积分':>6} {'上传视频':>8} {'平均评分':>8}\n")
        f.write("-" * 60 + "\n")
        for rank, stu in enumerate(students, 1):
            vids = [v for v in videos if v.user_id == stu.id]
            avg_r = (sum(v.average_rating for v in vids) / len(vids)) if vids else 0
            pts = stu.reward_points or 0
            f.write(f"{rank:<5} {stu.student_id:<16} {(stu.username or ''):<12} "
                    f"{pts:>6} {len(vids):>8} {avg_r:>8.2f}\n")

        f.write("\n\n")

        # 视频播放量
        f.write("【视频播放量 & 评分】\n")
        f.write(f"{'ID':<6} {'章':>3} {'题':>4} {'标题':<30} {'作者':<12} "
                f"{'播放':>6} {'评分':>6} {'收藏':>6} {'评论':>6}\n")
        f.write("-" * 90 + "\n")
        for v in videos:
            title = (v.title or '')[:28]
            author = (v.author.username if v.author else '')[:10]
            ch = v.chapter or '-'
            pn = v.problem_no or '-'
            views = v.views or 0
            avg_r = v.average_rating
            fav = len(v.favorites) if hasattr(v, 'favorites') else 0
            cmt = len(v.comments) if hasattr(v, 'comments') else 0
            f.write(f"{v.id:<6} {str(ch):>3} {str(pn):>4} {title:<30} {author:<12} "
                    f"{views:>6} {avg_r:>6.2f} {fav:>6} {cmt:>6}\n")

    # ─── 输出 CSV ─────────────────────────────────────────────────
    with open(out_csv, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)

        # Sheet 1：学生积分
        w.writerow(['=== 学生积分 ==='])
        w.writerow(['排名','学号','姓名','积分','上传视频数','平均评分'])
        for rank, stu in enumerate(students, 1):
            vids = [v for v in videos if v.user_id == stu.id]
            avg_r = round(sum(v.average_rating for v in vids) / len(vids), 2) if vids else 0
            w.writerow([rank, stu.student_id, stu.username or '',
                        stu.reward_points or 0, len(vids), avg_r])

        w.writerow([])
        # Sheet 2：视频明细
        w.writerow(['=== 视频明细 ==='])
        w.writerow(['视频ID','章','题','标题','作者','类型','播放次数','平均评分','收藏数','评论数','上传时间'])
        for v in videos:
            fav = len(v.favorites) if hasattr(v, 'favorites') else 0
            cmt = len(v.comments) if hasattr(v, 'comments') else 0
            w.writerow([
                v.id, v.chapter or '', v.problem_no or '',
                v.title or '', v.author.username if v.author else '',
                v.type or 'homework',
                v.views or 0, round(v.average_rating, 2),
                fav, cmt,
                v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else ''
            ])

    print(f"✅ 文本报告：{out_txt}")
    print(f"✅ CSV 报告：{out_csv}")

    # 控制台预览（前10名）
    print("\n学生积分前10：")
    print(f"{'排名':<5} {'学号':<16} {'姓名':<10} {'积分':>6}")
    print("-" * 42)
    for rank, stu in enumerate(students[:10], 1):
        print(f"{rank:<5} {stu.student_id:<16} {(stu.username or ''):<10} {(stu.reward_points or 0):>6}")

    print(f"\n视频总数：{len(videos)}，播放量统计：")
    total_views = sum(v.views or 0 for v in videos)
    top_viewed = sorted(videos, key=lambda v: v.views or 0, reverse=True)[:5]
    print(f"  总播放次数：{total_views}")
    print(f"  播放量TOP5：")
    for v in top_viewed:
        print(f"    [{v.id}] {v.title or ''}  播放:{v.views or 0}  评分:{v.average_rating:.2f}")
