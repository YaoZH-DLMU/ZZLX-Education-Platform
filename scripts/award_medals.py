"""
给全局评分最高的前3个作业视频颁发金银铜牌积分。

前提：已运行 reset_medals.py 清除旧记录。

用法：
  docker exec zzlxweb-web-1 python /app/scripts/award_medals.py
加 --dry-run 只预览不写库：
  docker exec zzlxweb-web-1 python /app/scripts/award_medals.py --dry-run
"""
import sys, os
sys.path.insert(0, '/app')

dry_run = '--dry-run' in sys.argv

from app import create_app
from app.models import db, Video, VideoRating, PointLog, PointSettlement
from sqlalchemy import func

app = create_app()

MEDAL_MAP = {
    0: ('medal_gold',   5.0, '🥇金牌'),
    1: ('medal_silver', 4.0, '🥈银牌'),
    2: ('medal_bronze', 3.0, '🥉铜牌'),
}

with app.app_context():
    # 全局按平均评分降序，取前3
    ranked = db.session.query(
        Video,
        func.avg(VideoRating.value).label('avg_r'),
        func.count(VideoRating.id).label('cnt_r')
    ).join(VideoRating, VideoRating.video_id == Video.id)\
     .filter(Video.type == 'homework')\
     .group_by(Video.id)\
     .having(func.count(VideoRating.id) >= 1)\
     .order_by(func.avg(VideoRating.value).desc())\
     .all()

    if not ranked:
        print("没有找到有评分的作业视频。")
        sys.exit(0)

    print(f"{'[DRY RUN] ' if dry_run else ''}全局前3名：\n")

    for rank, (video, avg_r, cnt_r) in enumerate(ranked[:3]):
        award_type, pts, label = MEDAL_MAP[rank]
        user = video.author
        author_name = user.username if user else '(无作者)'

        # 检查是否已有该牌型结算记录
        already = PointSettlement.query.filter_by(award_type=award_type).first()
        if already:
            print(f"  {label} 已有结算记录，跳过。")
            continue

        print(f"  {label}  {author_name}  《{video.title}》  "
              f"均分 {round(float(avg_r), 2)} ({cnt_r}人评分)  +{pts}分")

        if not dry_run and user:
            user.reward_points = (user.reward_points or 0) + pts
            db.session.add(PointLog(
                user_id=user.id, points=pts,
                reason=award_type, ref_video_id=video.id,
                memo=f'全局{label} 均分{round(float(avg_r),1)}（{video.title}）'
            ))
            db.session.add(PointSettlement(
                video_id=video.id, award_type=award_type,
                settled_by=user.id   # 系统操作，借用获奖者id
            ))

    if not dry_run:
        db.session.commit()
        print("\n✅ 金银铜牌积分已发放完毕。")
    else:
        print("\n[DRY RUN] 未写入数据库，以上仅为预览。")
