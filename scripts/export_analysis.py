"""
导出真实学生积分报告，并生成分析评价。
过滤条件：学号长度 >= 10（排除测试账号）

用法：
  docker exec zzlxweb-web-1 python /app/scripts/export_analysis.py
"""
import sys, os, json
from collections import defaultdict
sys.path.insert(0, '/app')

from app import create_app
from app.models import db, User, PointLog, Video, VideoRating, VideoComment

app = create_app()

with app.app_context():
    # 只取真实学生（学号10位，非教师）
    students = User.query.filter(
        User.is_teacher == False,
        db.func.length(User.student_id) >= 10
    ).order_by(User.reward_points.desc()).all()

    total = len(students)

    # ── 汇总每人数据 ──────────────────────────────────────────
    rows = []
    for u in students:
        logs = PointLog.query.filter_by(user_id=u.id).all()

        pts_by_type = defaultdict(float)
        for l in logs:
            pts_by_type[l.reason] += l.points

        sign_pts      = pts_by_type.get('sign_in', 0)
        interact_pts  = sum(v for k,v in pts_by_type.items() if k.startswith('interact'))
        medal_pts     = sum(pts_by_type.get(k,0) for k in ['medal_gold','medal_silver','medal_bronze'])
        closest_pts   = pts_by_type.get('closest_score', 0)
        champion_pts  = sum(pts_by_type.get(k,0) for k in ['champion_defend','champion_win'])

        video_count   = Video.query.filter_by(user_id=u.id, type='homework').count()
        rating_count  = VideoRating.query.filter_by(user_id=u.id).count()
        comment_count = VideoComment.query.filter_by(user_id=u.id).count()

        rows.append({
            'rank':       0,  # 填入后
            'name':       u.username,
            'student_id': u.student_id,
            'total':      round(u.reward_points or 0, 1),
            'sign':       round(sign_pts, 1),
            'interact':   round(interact_pts, 1),
            'medal':      round(medal_pts, 1),
            'closest':    round(closest_pts, 1),
            'champion':   round(champion_pts, 1),
            'videos':     video_count,
            'ratings':    rating_count,
            'comments':   comment_count,
        })

    for i, r in enumerate(rows, 1):
        r['rank'] = i

    # ── 写 CSV ────────────────────────────────────────────────
    import csv
    os.makedirs('/app/exports', exist_ok=True)
    csv_path = '/app/exports/student_analysis.csv'
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=[
            'rank','name','student_id','total',
            'sign','interact','medal','closest','champion',
            'videos','ratings','comments'
        ])
        w.writeheader()
        w.writerows(rows)
    print(f"✅ CSV 已保存：{csv_path}")

    # ── 生成文字报告 ──────────────────────────────────────────
    totals      = [r['total'] for r in rows]
    interacts   = [r['interact'] for r in rows]
    videos_list = [r['videos'] for r in rows]
    ratings_list= [r['ratings'] for r in rows]
    comments_list=[r['comments'] for r in rows]

    avg_total   = sum(totals)/total
    avg_inter   = sum(interacts)/total
    avg_video   = sum(videos_list)/total
    avg_rating  = sum(ratings_list)/total
    avg_comment = sum(comments_list)/total

    no_video    = sum(1 for v in videos_list if v == 0)
    low_rating  = sum(1 for v in ratings_list if v < 5)
    no_comment  = sum(1 for v in comments_list if v == 0)

    # 分段统计
    top10  = [r for r in rows if r['rank'] <= 10]
    mid    = [r for r in rows if 11 <= r['rank'] <= total//2]
    bottom = [r for r in rows if r['rank'] > total//2]

    medal_winners = [r for r in rows if r['medal'] > 0]
    champion_winners = [r for r in rows if r['champion'] > 0]

    txt_path = '/app/exports/student_report.txt'
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("          课堂参与与作业情况分析报告\n")
        f.write("=" * 60 + "\n\n")

        f.write(f"统计人数：{total} 人\n\n")

        f.write("【一、积分总体分布】\n")
        f.write(f"  平均积分：{avg_total:.1f} 分\n")
        f.write(f"  最高积分：{rows[0]['total']} 分（{rows[0]['name']}）\n")
        f.write(f"  最低积分：{rows[-1]['total']} 分（{rows[-1]['name']}）\n")
        f.write(f"  积分 ≥ 25 分：{sum(1 for t in totals if t>=25)} 人\n")
        f.write(f"  积分 20~25 分：{sum(1 for t in totals if 20<=t<25)} 人\n")
        f.write(f"  积分 < 20 分：{sum(1 for t in totals if t<20)} 人\n\n")

        f.write("【二、课堂互动参与情况】\n")
        f.write(f"  平均互动积分：{avg_inter:.1f} 分\n")
        f.write(f"  互动积分 ≥ 15 分（全勤）：{sum(1 for v in interacts if v>=15)} 人\n")
        f.write(f"  互动积分 10~15 分（良好）：{sum(1 for v in interacts if 10<=v<15)} 人\n")
        f.write(f"  互动积分 < 10 分（较少）：{sum(1 for v in interacts if v<10)} 人\n\n")

        f.write("【三、作业视频上传情况】\n")
        f.write(f"  平均上传视频数：{avg_video:.1f} 个\n")
        f.write(f"  上传 3 个及以上：{sum(1 for v in videos_list if v>=3)} 人\n")
        f.write(f"  上传 1~2 个：{sum(1 for v in videos_list if 1<=v<3)} 人\n")
        f.write(f"  未上传视频：{no_video} 人\n\n")

        f.write("【四、同学互评情况】\n")
        f.write(f"  平均评分次数：{avg_rating:.1f} 次\n")
        f.write(f"  平均评论次数：{avg_comment:.1f} 次\n")
        f.write(f"  评分不足 5 次（低于积分门槛）：{low_rating} 人\n")
        f.write(f"  从未评论过：{no_comment} 人\n\n")

        f.write("【五、优秀学生表现】\n")
        f.write("  金银铜牌获得者（作业视频全局前3）：\n")
        for r in medal_winners:
            f.write(f"    {r['name']}（{r['student_id']}）  牌奖 +{r['medal']}分\n")
        f.write(f"  擂主获得者（{len(champion_winners)}人）：\n")
        for r in champion_winners:
            f.write(f"    {r['name']}  守擂 +{r['champion']}分\n")
        f.write("\n")

        f.write("【六、积分前10名】\n")
        for r in top10:
            f.write(f"  {r['rank']:3d}. {r['name']:<10} {r['student_id']}  "
                    f"总{r['total']}分  "
                    f"(签到{r['sign']} 互动{r['interact']} 牌{r['medal']} 最近{r['closest']} 擂{r['champion']})\n")
        f.write("\n")

        f.write("【七、需关注的学生（积分后10名）】\n")
        bottom10 = sorted(rows, key=lambda r: r['total'])[:10]
        for r in bottom10:
            reasons = []
            if r['videos'] == 0: reasons.append("未上传视频")
            if r['ratings'] < 5: reasons.append(f"评分仅{r['ratings']}次")
            if r['comments'] == 0: reasons.append("未评论")
            reason_str = "、".join(reasons) if reasons else "参与度低"
            f.write(f"  {r['rank']:3d}. {r['name']:<10} {r['student_id']}  "
                    f"总{r['total']}分  [{reason_str}]\n")
        f.write("\n")

        f.write("=" * 60 + "\n")
        f.write("（报告由系统自动生成）\n")

    print(f"✅ 报告已保存：{txt_path}")
    print()
    # 控制台输出报告正文
    with open(txt_path, encoding='utf-8') as f:
        print(f.read())
