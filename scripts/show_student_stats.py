#!/usr/bin/env python3
"""
scripts/show_student_stats.py
输出所有学生的积分、有意义回复数、AI评分数据表
在容器内运行：docker exec zzlxweb-web-1 python /app/scripts/show_student_stats.py
"""
import os
import sqlite3
import sys
import json

for candidate in ['/app/db/app.db', '/app/app.db']:
    if os.path.exists(candidate):
        DB_PATH = candidate
        break
else:
    print('ERROR: 找不到数据库文件')
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

# ── 获取所有学生基础数据 ─────────────────────────────────────────
cur.execute('''
    SELECT u.id, u.username, u.student_id,
           COALESCE(u.reward_points, 0),
           COALESCE(u.meaningful_replies_count, 0)
    FROM user u
    WHERE u.is_teacher = 0
    ORDER BY COALESCE(u.reward_points, 0) DESC, u.student_id
''')
students = cur.fetchall()

# ── 获取每位学生视频的 AI 评分（取均值）──────────────────────────
cur.execute('''
    SELECT v.user_id,
           ROUND(AVG(a.score), 2),
           COUNT(a.id)
    FROM video_ai_score a
    JOIN video v ON v.id = a.video_id
    GROUP BY v.user_id
''')
ai_scores = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

# ── 积分明细（按类型汇总）────────────────────────────────────────
cur.execute('''
    SELECT user_id, reason, SUM(points)
    FROM point_log
    GROUP BY user_id, reason
''')
point_details = {}
for uid, reason, pts in cur.fetchall():
    point_details.setdefault(uid, {})[reason] = pts

# ── 输出表格 ──────────────────────────────────────────────────────
sep = '=' * 90
print(sep)
print(f'【学生综合数据报表】  DB: {DB_PATH}')
print(sep)
hdr = f'{"排名":>4}  {"姓名":<8}  {"学号":<14}  {"总积分":>6}  {"有意义回复":>8}  {"AI均分":>8}  {"AI评分视频":>8}'
print(hdr)
print('-' * 90)

for rank, (uid, uname, sid, pts, mr_cnt) in enumerate(students, 1):
    ai_avg, ai_cnt = ai_scores.get(uid, (None, 0))
    ai_str  = f'{ai_avg:.2f}' if ai_avg else '-'
    ai_cnt_str = str(ai_cnt) if ai_cnt else '-'
    print(f'{rank:>4}  {uname:<8}  {sid or "-":<14}  {pts:>6}  {mr_cnt:>8}  {ai_str:>8}  {ai_cnt_str:>8}')

print(sep)

# ── 积分明细汇总 ──────────────────────────────────────────────────
print('\n【积分来源汇总（含0分学生已省略）】')
type_totals = {}
for uid_pts in point_details.values():
    for reason, pts in uid_pts.items():
        type_totals[reason] = type_totals.get(reason, 0) + pts

print(f'  {"积分类型":<22}  {"总分":<8}')
print('  ' + '-' * 32)
for reason, total in sorted(type_totals.items(), key=lambda x: -x[1]):
    print(f'  {reason:<22}  {total:<8}')

# ── 总览统计 ─────────────────────────────────────────────────────
total_pts    = sum(r[3] for r in students)
total_mr     = sum(r[4] for r in students)
ai_scored    = len(ai_scores)

print(f'\n【总览】')
print(f'  学生总数: {len(students)}')
print(f'  总积分:   {total_pts}')
print(f'  总有意义回复: {total_mr}')
print(f'  已AI评分视频数: {ai_scored}')

conn.close()
