import json
import sqlite3
from collections import Counter


def _as_bool_int(value):
    return 1 if value in (1, True, '1', 'true', 'True') else 0


def main():
    conn = sqlite3.connect('/app/db/app.db')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    score_rows = cur.execute(
        '''
        SELECT vas.score, u.username, v.title, v.chapter, v.problem_no, vas.reason
        FROM video_ai_score vas
        JOIN video v ON v.id = vas.video_id
        JOIN user u ON u.id = v.user_id
        ORDER BY vas.score DESC, u.username ASC, v.id ASC
        '''
    ).fetchall()

    score_values = [float(r['score']) for r in score_rows]
    score_dist = Counter(score_values)
    score_stats = {
        'count': len(score_values),
        'min': min(score_values) if score_values else None,
        'max': max(score_values) if score_values else None,
        'avg': round(sum(score_values) / len(score_values), 4) if score_values else None,
        'distribution': [
            {'score': score, 'count': score_dist[score]}
            for score in sorted(score_dist.keys())
        ],
        'details': [
            {
                'score': float(r['score']),
                'username': r['username'],
                'title': r['title'],
                'chapter': r['chapter'],
                'problem_no': r['problem_no'],
                'reason': r['reason'] or '',
            }
            for r in score_rows
        ],
    }

    users = cur.execute(
        '''
        SELECT username, student_id, is_teacher, COALESCE(reward_points, 0) AS reward_points
        FROM user
        ORDER BY reward_points DESC, username ASC
        '''
    ).fetchall()

    point_values = [float(r['reward_points']) for r in users]
    if point_values:
        bucket_size = 10
        max_bucket = int(max(point_values) // bucket_size) * bucket_size
        buckets = []
        current = 0
        while current <= max_bucket:
            upper = current + bucket_size
            buckets.append({
                'label': f'{current}-{upper}',
                'count': sum(1 for value in point_values if current <= value < upper),
            })
            current = upper
    else:
        buckets = []

    point_stats = {
        'count': len(point_values),
        'min': min(point_values) if point_values else None,
        'max': max(point_values) if point_values else None,
        'avg': round(sum(point_values) / len(point_values), 4) if point_values else None,
        'teacher_count': sum(1 for r in users if _as_bool_int(r['is_teacher']) == 1),
        'student_count': sum(1 for r in users if _as_bool_int(r['is_teacher']) != 1),
        'buckets': buckets,
        'top_users': [
            {
                'username': r['username'],
                'student_id': r['student_id'],
                'is_teacher': _as_bool_int(r['is_teacher']),
                'reward_points': float(r['reward_points']),
            }
            for r in users[:20]
        ],
    }

    print(json.dumps({'score_stats': score_stats, 'point_stats': point_stats}, ensure_ascii=False))
    conn.close()


if __name__ == '__main__':
    main()