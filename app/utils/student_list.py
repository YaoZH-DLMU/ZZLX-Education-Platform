"""
学生名单工具函数
================
读取项目根目录下 StudentList.txt（每行格式：学号 空格 姓名）
若文件不存在则不做限制，所有学号均视为有效。
"""
import os


def _list_path():
    """返回 StudentList.txt 的绝对路径（按课程分流）"""
    from app.utils.course_paths import student_list_path
    return student_list_path()


def load_student_list() -> dict:
    """
    返回 {学号: 姓名} 字典。
    文件不存在或读取失败时返回空字典（空字典 = 不限制）。
    """
    try:
        p = _list_path()
        if not os.path.exists(p):
            return {}
        result = {}
        with open(p, encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split(None, 1)
                if len(parts) >= 2:
                    result[parts[0]] = parts[1]
                elif len(parts) == 1:
                    result[parts[0]] = ''
        return result
    except Exception:
        return {}


def validate_student_id(student_id: str):
    """
    检验学号是否在名单中。
    返回 (valid: bool, message: str)。
    文件不存在时始终返回 (True, '')。
    """
    lst = load_student_list()
    if not lst:          # 无名单文件 → 不做限制
        return True, ''
    if student_id not in lst:
        return False, '请输入准确的学号（不在学生名单中）'
    return True, ''
