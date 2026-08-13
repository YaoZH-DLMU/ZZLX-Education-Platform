#!/usr/bin/env python3
"""
customize_site.py - 致知力行平台 多课程配置工具（重写版）
========================================================

旧版通过字符串替换直接改模板/nginx 源码来生成课程变体，脆弱且不可逆。
重构后所有课程差异集中在 app/courses.py 的 COURSES 注册表，模板与后端
运行时读取。本脚本只做两件事：

  1. --list            列出全部课程的配置概览
  2. --reinit-forum KEY 打印重置某课程论坛板块的命令（需在 Flask 上下文执行）

新增/修改一门课：直接编辑 app/courses.py 里的 COURSES 字典即可，
无需运行本脚本，刷新浏览器即生效（模板自动读取 site.*）。

用法：
  python customize_site.py --list
  python customize_site.py --reinit-forum zzlx
"""

import argparse


def list_courses():
    from app.courses import COURSES
    for c in COURSES.values():
        print(f"\n═══ [{c['key']}]  前缀 {c['prefix']}  ═══")
        print(f"  site_title      : {c['site_title']}")
        print(f"  logo_alt        : {c['logo_alt']}")
        print(f"  platform_name   : {c['platform_name']}")
        print(f"  nav             : 朋辈={c['nav']['homework']}  同心={c['nav']['defense']}"
              f"  图谱={c['nav']['graph']}  论坛={c['nav']['forum']}")
        print(f"  ai_name         : {c['ai_name']}")
        print(f"  ai_persona      : {c['ai_persona']}")
        print(f"  student_list    : {c['student_list']}")
        print(f"  crane/dumper    : {c['crane_name']} / {c['dumper_name']}")
        print(f"  video_type      : 作业={c['video_type_homework']}  翻转={c['video_type_defense']}")
        print(f"  forum_boards    : {[b[0] for b in c['forum_boards']]}")


def reinit_forum_cmd(course_key):
    from app.courses import COURSES
    if course_key not in COURSES:
        print(f"未知课程 key：{course_key}（可选：{', '.join(COURSES)}）")
        return
    c = COURSES[course_key]
    boards = ", ".join([f"('{n}', '{d}')" for n, d in c['forum_boards']])
    print(f"\n重置 [{course_key}] 论坛板块命令（容器内执行）：\n")
    print(f"""  docker exec zzlxweb-web-1 python3 -c "
from app import create_app, db
from app.models import ForumBoard
app = create_app()
with app.app_context():
    ForumBoard.query.delete()
    for name, desc in [{boards}]:
        db.session.add(ForumBoard(name=name, description=desc))
    db.session.commit()
    print('论坛板块已重置为：' + {[b[0] for b in c['forum_boards']]})
"
""")
    print("注意：多课镜像下需指定该课程的 DB（Phase 2 落地后）。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="致知力行平台 多课程配置工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
课程差异均在 app/courses.py 的 COURSES 注册表中维护，编辑后刷新即生效。
本脚本仅用于查看配置与重置论坛板块。
        """,
    )
    parser.add_argument("--list", action="store_true", help="列出全部课程配置概览")
    parser.add_argument("--reinit-forum", metavar="KEY", help="打印重置某课程论坛板块的命令")
    args = parser.parse_args()

    if args.list:
        list_courses()
    elif args.reinit_forum:
        reinit_forum_cmd(args.reinit_forum)
    else:
        parser.print_help()
