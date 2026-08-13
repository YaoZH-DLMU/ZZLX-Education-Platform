#!/usr/bin/env python3
"""
cleanup_files.py - 服务器文件清理脚本
=====================================
1. 签到md清理：emotion_curve.json 已生成时，旧md文件可删除（json已含全部数据）
2. 部署包清理：删除项目根目录的 *-deploy.tar.gz
3. 无关联视频：检查 uploads/ 下的视频文件，未关联到 DB 中 Video 记录的移到 _orphan/ 供人工核查

用法（容器内）：
  docker compose exec web python /app/scripts/cleanup_files.py [--dry-run] [--delete-md] [--delete-tar] [--move-orphan]

选项：
  --dry-run       只列出，不实际操作
  --delete-md     删除签到md（默认只列出）
  --delete-tar    删除部署包（默认只列出）
  --move-orphan   移动无关联视频到 _orphan/（默认只列出）
  --all           执行全部清理（= --delete-md --delete-tar --move-orphan）
"""
import os, sys, glob, json

sys.path.insert(0, '/app')
DRY_RUN = '--dry-run' in sys.argv
DO_ALL = '--all' in sys.argv
DO_MD = DO_ALL or '--delete-md' in sys.argv
DO_TAR = DO_ALL or '--delete-tar' in sys.argv
DO_ORPHAN = DO_ALL or '--move-orphan' in sys.argv


def main():
    from app import create_app, db
    from app.models import Video
    app = create_app()
    with app.app_context():
        uploads = os.path.join(app.root_path, 'static', 'uploads')
        sign_dir = os.path.join(app.root_path, 'exports', 'sign')
        root = os.path.dirname(os.path.dirname(app.root_path))  # /opt/ZZLXWeb

        print('='*60)
        print(f'文件清理 {"[预览模式]" if DRY_RUN else "[执行模式]"}')
        print('='*60)

        # ── 1. 签到md清理 ──
        print('\n📋 1. 签到md文件')
        curve_json = os.path.join(sign_dir, 'emotion_curve.json')
        if os.path.exists(curve_json):
            mds = glob.glob(os.path.join(sign_dir, '*.md'))
            print(f'  emotion_curve.json 已存在（{len(mds)} 个md文件可清理）')
            for md in sorted(mds):
                print(f'    {os.path.basename(md)}')
                if DO_MD and not DRY_RUN:
                    os.remove(md)
            if DO_MD and not DRY_RUN:
                print(f'  ✅ 已删除 {len(mds)} 个md文件')
        else:
            print('  emotion_curve.json 不存在，跳过md清理')

        # ── 2. 部署包清理 ──
        print('\n📦 2. 部署包文件')
        tars = glob.glob(os.path.join(root, '*-deploy.tar.gz')) + \
               glob.glob(os.path.join(root, 'deploy_*.zip'))
        if tars:
            print(f'  发现 {len(tars)} 个部署包：')
            for t in sorted(tars):
                sz = os.path.getsize(t) / 1024
                print(f'    {os.path.basename(t)} ({sz:.0f}KB)')
                if DO_TAR and not DRY_RUN:
                    os.remove(t)
            if DO_TAR and not DRY_RUN:
                print(f'  ✅ 已删除 {len(tars)} 个部署包')
        else:
            print('  无部署包')

        # ── 3. 无关联视频文件 ──
        print('\n🎬 3. 无关联视频文件')
        # DB 中所有视频文件名
        db_files = set()
        for v in Video.query.all():
            if v.url:
                fname = os.path.basename(v.url.split('?')[0])
                db_files.add(fname)

        # 扫描 uploads/ 下的视频文件
        video_exts = {'.mp4', '.webm', '.ogg', '.avi', '.mov', '.mkv'}
        orphan_dir = os.path.join(uploads, '_orphan')
        orphan_count = 0
        total_count = 0
        for fname in sorted(os.listdir(uploads)):
            fpath = os.path.join(uploads, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in video_exts:
                continue
            total_count += 1
            if fname not in db_files:
                orphan_count += 1
                print(f'    [无关联] {fname}')
                if DO_ORPHAN and not DRY_RUN:
                    os.makedirs(orphan_dir, exist_ok=True)
                    os.rename(fpath, os.path.join(orphan_dir, fname))

        print(f'\n  视频文件总计: {total_count}')
        print(f'  DB关联: {total_count - orphan_count}')
        print(f'  无关联: {orphan_count}')
        if DO_ORPHAN and not DRY_RUN and orphan_count:
            print(f'  ✅ 已移动 {orphan_count} 个无关联视频到 _orphan/')

        # ── 汇总 ──
        print('\n' + '='*60)
        if DRY_RUN:
            print('预览完成。加 --all 执行清理，或单独加 --delete-md / --delete-tar / --move-orphan')
        else:
            done = []
            if DO_MD: done.append('md')
            if DO_TAR: done.append('部署包')
            if DO_ORPHAN: done.append('无关联视频')
            if done:
                print(f'已清理: {", ".join(done)}')
            else:
                print('未执行清理（加 --all 或具体选项）')

        # _orphan 目录提示
        if os.path.isdir(orphan_dir):
            orphan_files = os.listdir(orphan_dir)
            if orphan_files:
                print(f'\n⚠️  _orphan/ 目录有 {len(orphan_files)} 个文件待你核查。')
                print('   确认无误后手动删除: docker compose exec web rm -rf /app/app/static/uploads/_orphan')


if __name__ == '__main__':
    main()
