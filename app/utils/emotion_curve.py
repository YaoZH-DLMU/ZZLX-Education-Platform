"""
app/utils/emotion_curve.py
情感曲线 JSON 管理（读取/追加/删除节点）
========================================
数据文件：app/exports/sign/emotion_curve.json
格式：[ {date, title, participants, acceptance, interest, burden, autonomy, summary, tags, ...}, ... ]
"""

import os, json
from flask import current_app


def _curve_path():
    from app.utils.course_paths import exports_dir
    return os.path.join(exports_dir('sign'), 'emotion_curve.json')


def load_curve():
    """读取全部节点。"""
    path = _curve_path()
    if not os.path.exists(path):
        return []
    try:
        return json.loads(open(path, encoding='utf-8').read())
    except Exception:
        return []


def save_curve(nodes):
    """保存全部节点。"""
    path = _curve_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(nodes, f, ensure_ascii=False, indent=2)


def append_node(node):
    """追加一个节点（去重：同 title 保留最新）。AI 直接打分，无需归一化。"""
    nodes = load_curve()
    nodes = [n for n in nodes if n.get('title') != node.get('title')]
    nodes.append(node)
    save_curve(nodes)
    return True


def remove_node(md_filename):
    """按 md_file 删除节点。"""
    nodes = load_curve()
    before = len(nodes)
    nodes = [n for n in nodes if n.get('md_file') != md_filename]
    if len(nodes) < before:
        save_curve(nodes)
        return True
    return False
