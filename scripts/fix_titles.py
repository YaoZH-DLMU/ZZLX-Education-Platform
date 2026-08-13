import sqlite3, re

db = '/var/lib/docker/volumes/zzlxweb_zzlx_db/_data/app.db'
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("SELECT id, title FROM video WHERE type IN ('flipped', 'cooperation', 'defense')")
rows = cur.fetchall()
updated = 0
for vid, title in rows:
    new_title = title
    # 立卷夹钳: ...夹钳...第X小组...第Y次答辩 -> 夹钳_第X小组第Y次答辩
    m = re.search(r'夹钳.*?第(\d+)小组.*?第(\d+)次答辩', title)
    if m:
        new_title = f'夹钳_第{m.group(1)}小组第{m.group(2)}次答辩'
    else:
        # 翻车机: ...翻车机...第M小组...第N次答辩 -> 翻车机_第M小组第N次答辩
        m2 = re.search(r'翻车机.*?第(\d+)小组.*?第(\d+)次答辩', title)
        if m2:
            new_title = f'翻车机_第{m2.group(1)}小组第{m2.group(2)}次答辩'
    if new_title != title:
        cur.execute('UPDATE video SET title=? WHERE id=?', (new_title, vid))
        print(f'  [{vid}] {title}  ->  {new_title}')
        updated += 1
con.commit()
con.close()
print(f'Done: {updated} titles updated.')
