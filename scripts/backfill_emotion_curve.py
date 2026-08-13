#!/usr/bin/env python3
"""
backfill_emotion_curve.py - 一次性回填：扫描签到 md，去重，AI 打分+摘要，生成 emotion_curve.json。

用法（容器内）：
  docker compose cp scripts/backfill_emotion_curve.py web:/app/scripts/backfill_emotion_curve.py
  docker compose exec web python /app/scripts/backfill_emotion_curve.py
"""
import os, sys, glob
sys.path.insert(0, '/app')

def main():
    from app import create_app
    from app.utils.emotion_score import score_md_file
    from app.utils.emotion_curve import save_curve

    app = create_app()
    with app.app_context():
        md_dir = os.path.join(app.root_path, 'exports', 'sign')
        mds = sorted(glob.glob(os.path.join(md_dir, '*.md')))
        print(f'扫描到 {len(mds)} 个 md 文件')

        # 第一遍：解析+去重（同 title 保留最新 date）
        candidates = {}  # title -> (date, md_path)
        for md in mds:
            from app.utils.emotion_score import parse_md, count_real_students
            parsed = parse_md(md)
            if not parsed:
                continue
            real = count_real_students(parsed.get('student_ids', []))
            if real < 10:
                continue
            title = parsed['question'][:60]
            date = parsed.get('export_time', '')[:10]
            if title not in candidates or date >= candidates[title][0]:
                candidates[title] = (date, md)

        print(f'有效签到（≥10真实学生，去重后）: {len(candidates)} 个')

        # 第二遍：AI 打分+摘要（按日期排序，传入上次分数保持轨迹连续性）
        nodes = []
        prev = None
        for i, (title, (date, md)) in enumerate(sorted(candidates.items(), key=lambda x: x[1][0])):
            print(f'  [{i+1}/{len(candidates)}] {date} {title[:30]}...', end=' ', flush=True)
            node = score_md_file(md, prev_scores=prev)
            if node:
                nodes.append(node)
                prev = node  # 传给下一次作为轨迹上下文
                print(f'✅ A={node["acceptance"]} I={node["interest"]} B={node["burden"]} Au={node["autonomy"]}')
            else:
                print('❌ 跳过（AI失败或非课程话题）')

        save_curve(nodes)
        print(f'\n完成：emotion_curve.json 保存 {len(nodes)} 个节点')

if __name__ == '__main__':
    main()
