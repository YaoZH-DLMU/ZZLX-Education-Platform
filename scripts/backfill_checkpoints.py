#!/usr/bin/env python3
"""
backfill_checkpoints.py - 给已转译但缺检查点的历史视频补 checkpoint_sec/duration_sec + 写转译文件。

用法（容器内）：
  docker compose cp scripts/backfill_checkpoints.py web:/app/scripts/backfill_checkpoints.py
  docker compose exec web python /app/scripts/backfill_checkpoints.py
"""
import os, sys, subprocess, json, random

sys.path.insert(0, '/app')

def main():
    from app import create_app, db
    from app.models import Video, VideoAIScore
    app = create_app()
    with app.app_context():
        rows = VideoAIScore.query.filter(VideoAIScore.checkpoint_sec.is_(None)).all()
        print(f'待回填: {len(rows)} 条')
        done = 0
        for ai in rows:
            v = Video.query.get(ai.video_id)
            if not v:
                print(f'  skip video#{ai.video_id} (DB 记录缺失)')
                continue
            # 视频文件路径：url 形如 /static/uploads/xxx.mp4
            fn = (v.url or '').split('/uploads/')[-1]
            path = '/app/app/static/uploads/' + fn
            if not fn or not os.path.exists(path):
                print(f'  skip video#{v.id} (文件缺失: {path})')
                continue
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', path],
                    capture_output=True, timeout=30)
                dur = float(r.stdout.decode().strip())
            except Exception as e:
                print(f'  skip video#{v.id} (ffprobe 失败: {e})')
                continue
            if dur < 20:
                print(f'  skip video#{v.id} (时长 {dur:.1f}s < 20s，不生成检查点)')
                continue
            cp = round(random.uniform(0.1 * dur, 0.9 * dur), 2)
            ai.duration_sec = dur
            ai.checkpoint_sec = cp
            # 写转译文件
            try:
                d = os.path.join(app.root_path, 'exports', 'video_transcripts')
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f'{v.id}.json'), 'w', encoding='utf-8') as f:
                    json.dump({'video_id': v.id, 'title': v.title, 'duration': dur,
                               'checkpoint': cp, 'score': ai.score,
                               'transcript': ai.transcript or ''}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f'  warn video#{v.id} 写转译文件失败: {e}')
            done += 1
            print(f'  ✅ video#{v.id} {v.title} -> cp {cp}s / {dur:.1f}s')
        db.session.commit()
        print(f'完成：回填 {done} 条')

if __name__ == '__main__':
    main()
