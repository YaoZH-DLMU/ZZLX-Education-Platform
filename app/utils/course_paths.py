"""
app/utils/course_paths.py
多课程文件路径助手
========================================
zzlx 用原有路径（零迁移），其他课加 course_key 子目录/前缀。
"""

import os
from flask import g, current_app


def upload_folder():
    """上传根目录。zzlx 用原路径，其他课加子目录。"""
    key = getattr(g, 'course_key', 'zzlx')
    base = current_app.config['UPLOAD_FOLDER']
    return base if key == 'zzlx' else os.path.join(base, key)


def kg_path():
    """知识图谱 KG.json 路径。zzlx 用 /app/KG.json，其他课用 /app/KG_{key}.json。
    支持课程间共享（如航海力学共享工程力学的 KG）：courses.py 里设 kg_shared_with。"""
    from app.courses import get_course
    course = get_course()
    key = course.get('kg_shared_with') or course.get('key', 'zzlx')
    return '/app/KG.json' if key == 'zzlx' else f'/app/KG_{key}.json'


def qg_path():
    """问题图谱 QG.json 路径。支持共享（同 kg_path）。"""
    from app.courses import get_course
    course = get_course()
    key = course.get('kg_shared_with') or course.get('key', 'zzlx')
    return '/app/QG.json' if key == 'zzlx' else f'/app/QG_{key}.json'


def student_list_path():
    """学号名单文件路径。zzlx 用原文件，其他课加 _{key} 后缀。"""
    key = getattr(g, 'course_key', 'zzlx')
    base = os.path.join(current_app.root_path, '..')
    return os.path.join(base, 'StudentList.txt') if key == 'zzlx' \
           else os.path.join(base, f'StudentList_{key}.txt')


def exports_dir(subdir):
    """导出目录。zzlx 用原路径 exports/{subdir}，其他课用 exports/{key}/{subdir}。"""
    key = getattr(g, 'course_key', 'zzlx')
    base = os.path.join(current_app.root_path, 'exports')
    return os.path.join(base, subdir) if key == 'zzlx' \
           else os.path.join(base, key, subdir)


def transcript_dir():
    """视频转译文件目录。"""
    key = getattr(g, 'course_key', 'zzlx')
    base = os.path.join(current_app.root_path, 'exports', 'video_transcripts')
    return base if key == 'zzlx' \
           else os.path.join(current_app.root_path, 'exports', key, 'video_transcripts')
