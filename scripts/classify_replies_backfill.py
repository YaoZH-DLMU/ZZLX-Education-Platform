#!/usr/bin/env python3
"""
scripts/classify_replies_backfill.py
批量对所有作业视频评论(video_comment)进行 AI 质量分类，
更新 is_meaningful 字段及用户有意义回复数。
在容器内运行：docker exec zzlxweb-web-1 python /app/scripts/classify_replies_backfill.py
"""
import os
import sys
import sqlite3
import difflib
from pathlib import Path

# ── DB 路径 ─────────────────────────────────────────────────────
for candidate in ['/app/db/app.db', '/app/app.db']:
    if os.path.exists(candidate):
        DB_PATH = candidate
        break
else:
    print('ERROR: 找不到数据库文件')
    sys.exit(1)

# ── API Key ──────────────────────────────────────────────────────
api_key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
if not api_key:
    kf = Path('/app/APIKey.txt')
    if kf.exists():
        api_key = kf.read_text(encoding='utf-8').strip()
if not api_key:
    print('ERROR: 未找到 DeepSeek API Key（环境变量 DEEPSEEK_API_KEY 或 /app/APIKey.txt）')
    sys.exit(1)

from openai import OpenAI
client = OpenAI(api_key=api_key, base_url='https://api.deepseek.com')

# ── 分类提示词 ────────────────────────────────────────────────────
_PROMPT = """你是一个教学平台回复质量评审助手。
请判断以下学生的讨论回复是否属于"有意义的回复"。

【无意义的判定标准】（满足任意一条即为无意义）：
1. 内容为空或纯符号/标点
2. 单纯的数字、字母组合：如"123""abc""666"
3. 过短（不足20字）且无实质信息量：如"好""嗯""哦""是的""讲得很好"
4. 完全重复无意义的词：如"哈哈哈哈""啊啊啊"

【有意义的回复】（以下均算有意义）：
1. 包含具体讨论、问题或补充知识点
2. 包含具体的评价描述，说明了哪里好/哪里需改进（即使是表扬，只要提到了具体点，通常需20字以上才能说清楚）
3. 提供建议或解析
4. 长度在20字以上且表达了明确意思

请仅回答 YES（有意义）或 NO（无意义），不要输出任何其他内容。

回复内容：{text}"""


def _trivially_meaningless(text: str):
    """本地快速预判，返回 (True, reason) 或 (False, '')"""
    s = text.strip()
    if not s:
        return True, '内容为空'
    if len(s) < 20:
        return True, f'字数不足20字（实际 {len(s)} 字）：「{s[:30]}」'
    if s.isdigit():
        return True, f'纯数字（「{s}」）'
    if s.isascii() and s.replace(' ', '').isalpha() and len(s.replace(' ', '')) <= 5:
        return True, f'纯英文字母不超过5位（「{s}」）'
    # 常见无意义词列表（完全匹配）
    trivial_words = {'好', '嗯', 'ok', 'OK', '哈哈', '哦', '噢', '对', '是',
                     '好的', '收到', '明白', '了解', '谢谢', '感谢',
                     '666', '666!', '哈哈哈', '加油', '👍', '😊'}
    if s in trivial_words:
        return True, f'无意义词汇（「{s}」）'
    return False, ''


def classify(text: str):
    """
    返回 (is_meaningful: bool, reason: str, category: str)
    category: 'trivial'=字数不够 | 'irrelevant'=无关联性 | 'ok'=有意义
    """
    trivial, reason = _trivially_meaningless(text)
    if trivial:
        return False, reason, 'trivial'

    try:
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': _PROMPT.format(text=text.strip())}],
            temperature=0,
            max_tokens=5,
        )
        answer = resp.choices[0].message.content.strip().upper()
        if answer.startswith('Y'):
            return True, '', 'ok'
        else:
            return False, '没有关联性或没有指出具体问题', 'irrelevant'
    except Exception as e:
        print(f'  [WARN] API调用异常: {e}，默认视为有意义')
        return True, '', 'ok'


# ── 主逻辑 ────────────────────────────────────────────────────────
def main():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # 查所有【作业视频评论】及其作者信息（仅 homework 类型视频）
    cur.execute('''
        SELECT vc.id, vc.content, vc.user_id, u.username, u.student_id,
               v.title, v.chapter
        FROM video_comment vc
        JOIN user u ON u.id = vc.user_id
        JOIN video v ON v.id = vc.video_id
        WHERE v.type = 'homework' OR v.type IS NULL
        ORDER BY vc.id
    ''')
    replies = cur.fetchall()

    print(f'数据库: {DB_PATH}')
    print(f'共 {len(replies)} 条作业评论，开始批量分类...\n')

    results   = {}   # reply_id → (is_meaningful, reason, category)
    meaningless_list = []
    # 用于查重：user_id → [(rid, video_id, content, vtitle, vchapter)]
    user_meaningful = {}

    for i, (rid, content, author_id, username, student_id, vtitle, vchapter) in enumerate(replies):
        is_m, reason, cat = classify(content)
        results[rid] = (is_m, reason, cat)

        if not is_m:
            meaningless_list.append({
                'id':      rid,
                'content': content[:100].replace('\n', ' '),
                'author':  f'{username}（{student_id or "-"}）',
                'video':   f'{vtitle or ""} 第{vchapter}章' if vchapter else vtitle or '',
                'reason':  reason,
                'cat':     cat,
            })
        else:
            # 收集有意义评论用于查重
            user_meaningful.setdefault(author_id, {
                'name': f'{username}（{student_id or "-"}）',
                'items': [],
            })['items'].append((rid, content, vtitle or '', vchapter or ''))

        if (i + 1) % 20 == 0 or (i + 1) == len(replies):
            ok_n = sum(1 for v in results.values() if v[0])
            no_n = len(results) - ok_n
            print(f'  [{i+1}/{len(replies)}] 有意义={ok_n}  无意义={no_n}')

    # ── 更新 video_comment.is_meaningful ──────────────────────────
    print('\n写入 is_meaningful 字段...')
    for rid, (is_m, _, _) in results.items():
        cur.execute('UPDATE video_comment SET is_meaningful=? WHERE id=?',
                    (1 if is_m else 0, rid))

    # ── 重新统计每位学生的有意义回复数 ────────────────────────────
    cur.execute('''
        SELECT vc.user_id, COUNT(*)
        FROM video_comment vc
        JOIN video v ON v.id = vc.video_id
        WHERE vc.is_meaningful = 1
          AND (v.type = 'homework' OR v.type IS NULL)
        GROUP BY vc.user_id
    ''')
    counts = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute('SELECT id FROM user WHERE is_teacher = 0')
    for (uid,) in cur.fetchall():
        cur.execute('UPDATE user SET meaningful_replies_count=? WHERE id=?',
                    (counts.get(uid, 0), uid))

    conn.commit()
    conn.close()

    # ── 输出报告 ──────────────────────────────────────────────────
    total       = len(replies)
    meaningful  = total - len(meaningless_list)
    trivial_n   = sum(1 for m in meaningless_list if m['cat'] == 'trivial')
    irrel_n     = sum(1 for m in meaningless_list if m['cat'] == 'irrelevant')

    sep = '=' * 70
    print(f'\n{sep}')
    print(f'【批量分类完成】总计: {total}  有意义: {meaningful}  无意义: {len(meaningless_list)}')
    print(f'  其中 ① 字数不够: {trivial_n}  ② 无关联性/未指出问题: {irrel_n}')
    print(sep)

    if meaningless_list:
        print('\n【无意义评论明细】')
        print(f'{"ID":>5}  {"作者":<18}  {"视频":<16}  {"原因":<10}  内容摘要')
        print('-' * 85)
        for m in meaningless_list:
            cat_label = '①字数不足' if m['cat'] == 'trivial' else '②无关联性'
            print(f'{m["id"]:>5}  {m["author"]:<18}  {m["video"]:<16}  {cat_label:<10}  「{m["content"]}」')
    else:
        print('\n没有无意义评论，全部通过！')

    # ── 查重报告：同一用户对不同视频提交相似内容 ──────────────────
    DUP_THRESHOLD = 0.80
    dup_users = {}
    for uid, udata in user_meaningful.items():
        items = udata['items']
        if len(items) < 2:
            continue
        flagged_pairs = []
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                rid_a, c_a, vt_a, vc_a = items[a]
                rid_b, c_b, vt_b, vc_b = items[b]
                ratio = difflib.SequenceMatcher(None, c_a.strip(), c_b.strip()).ratio()
                if ratio >= DUP_THRESHOLD:
                    flagged_pairs.append((ratio, rid_a, c_a, vt_a, vc_a, rid_b, c_b, vt_b, vc_b))
        if flagged_pairs:
            dup_users[uid] = {'name': udata['name'], 'pairs': flagged_pairs}

    if dup_users:
        print(f'\n{"=" * 70}')
        print(f'【查重警告】发现 {len(dup_users)} 名学生在不同视频提交了高度相似的评论（相似度≥{int(DUP_THRESHOLD*100)}%）')
        print('（仅报告，未自动标记为无意义，请人工核查）')
        print('-' * 70)
        for uid, udata in dup_users.items():
            print(f'\n  ▶ {udata["name"]}  共 {len(udata["pairs"])} 对相似评论：')
            for ratio, rid_a, c_a, vt_a, vc_a, rid_b, c_b, vt_b, vc_b in udata['pairs']:
                vid_a = f'{vt_a} 第{vc_a}章' if vc_a else vt_a
                vid_b = f'{vt_b} 第{vc_b}章' if vc_b else vt_b
                print(f'    相似度 {ratio:.0%}')
                print(f'      [#{rid_a}] {vid_a}：「{c_a[:60]}」')
                print(f'      [#{rid_b}] {vid_b}：「{c_b[:60]}」')
    else:
        print('\n【查重】未发现高度相似的重复评论。')

    print(f'\n{sep}')
    print('已同步更新 video_comment.is_meaningful 及 user.meaningful_replies_count 字段。')


if __name__ == '__main__':
    main()
