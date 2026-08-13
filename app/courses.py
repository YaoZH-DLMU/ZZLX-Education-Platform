"""
app/courses.py
多课程（多课镜像）配置注册表
========================================

平台采用「单应用 + 每课独立 DB + URL 前缀路由」架构。
本模块集中存放每门课的差异化配置。
"""

from flask import g, has_request_context

DEFAULT_COURSE = 'zzlx'


def _zzlx():
    """材料力学（现网主课）。"""
    return {
        'key':             'zzlx',
        'prefix':          '/ZZLX',
        'bind':            'zzlx',
        'site_title':      '学习互助平台',
        'logo_alt':        '致知力行+AI',
        'platform_name':   '致知力行学习平台',
        'course_name':     '材料力学',
        'nav': {
            'homework': '朋辈助力',
            'defense':  '同心协力',
            'graph':    '力学笃行',
            'forum':    '力排万难',
        },
        'ai_name':         '知知',
        'ai_panel_title':  '知知 · 力学助手',
        'ai_intro':        '有关理论力学或材料力学的问题，随时来问我！',
        'ai_persona':      '大学本科阶段的【理论力学】和【材料力学】',
        'student_list':    'StudentList.txt',
        'crane_name':      '双臂立卷夹钳',
        'dumper_name':     '全机械式翻车机',
        'video_type_homework': '朋辈助力',
        'video_type_defense':  '翻转课堂',
        # 作业章节列表（按课程不同）
        'homework_chapters':   [2, 3, 4, 5, 6, 7, 8, 9, 10, 13],  # 材料力学跳过1/11/12
        # 作业上传：科目选择
        'upload_subjects':    ['理论力学', '材料力学'],  # 表单显示的科目选项
        'upload_subject_fixed': None,                    # 不固定（学生选）
        'title_subject':      None,                      # 标题用学生选的 subject
        'mobile_short':       '材力',                    # 手机端短标题前缀
        # KG/QG 共享（航海力学和工程力学共用）
        'kg_shared_with':     None,                      # 不共享，用自己的
        'forum_boards': [
            ('智慧学习', 'AI技术、课程建议，聊科技畅未来'),
            ('案例分析', '进阶式案例的分析与设计'),
            ('力学畅想', '生活中的力学现象与思考'),
        ],
    }


def _lllx():
    """理论力学（含静力学、运动学、动力学）。"""
    c = _zzlx()
    c.update({
        'key':                'lllx',
        'prefix':             '/LLLX',
        'bind':               'lllx',
        'site_title':         '理论力学学习平台',
        'platform_name':      '理论力学学习平台',
        'course_name':        '理论力学',
        'student_list':       'StudentList_lllx.txt',
        # 翻转课堂：和材料力学一致
        'crane_name':         '双臂立卷夹钳',
        'dumper_name':        '全机械式翻车机',
        # 作业上传：固化理论力学
        'upload_subjects':    ['理论力学'],
        'upload_subject_fixed': '理论力学',
        'title_subject':      '理论力学',
        'mobile_short':       '理力',
        'homework_chapters':  list(range(1, 15)),  # 理论力学 1-14 章
    })
    return c


def _gclx():
    """工程力学（含静力学、运动学、材料力学基本变形、压杆稳定、强度理论、组合变形）。"""
    c = _zzlx()
    c.update({
        'key':                'gclx',
        'prefix':             '/GCLX',
        'bind':               'gclx',
        'site_title':         '工程力学学习平台',
        'platform_name':      '工程力学学习平台',
        'course_name':        '工程力学',
        'student_list':       'StudentList_gclx.txt',
        # 翻转课堂
        'crane_name':         '工程力学案例1',
        'dumper_name':        '工程力学案例2',
        # 作业上传：无科目选择
        'upload_subjects':    [],
        'upload_subject_fixed': None,
        'title_subject':      '工程力学',
        'mobile_short':       '力学',
        'homework_chapters':  list(range(1, 14)),  # 工程力学 1-13 章
    })
    return c


def _hhlx():
    """航海力学（简版工程力学，共用工程力学 KG/QG）。"""
    c = _gclx()  # 以工程力学为基础
    c.update({
        'key':                'hhlx',
        'prefix':             '/HHLX',
        'bind':               'hhlx',
        'site_title':         '航海力学学习平台',
        'platform_name':      '航海力学学习平台',
        'course_name':        '航海力学',
        'student_list':       'StudentList_hhlx.txt',
        # 翻转课堂
        'crane_name':         '航海力学案例1',
        'dumper_name':        '航海力学案例2',
        'title_subject':      '航海力学',
        'mobile_short':       '力学',
        'homework_chapters':  list(range(1, 14)),  # 航海力学 1-13 章（同工程力学）
        # KG/QG 和工程力学共用
        'kg_shared_with':     'gclx',
    })
    return c


COURSES = {
    'zzlx': _zzlx(),
    'lllx': _lllx(),
    'gclx': _gclx(),
    'hhlx': _hhlx(),
}


def list_courses():
    """列出全部课程（供落地页渲染）。"""
    return list(COURSES.values())


def get_course(key=None):
    """取课程配置；key 为空时取当前请求的课程。"""
    if key is None:
        if has_request_context():
            key = getattr(g, 'course_key', None)
        if key is None:
            key = DEFAULT_COURSE
    return COURSES.get(key, COURSES[DEFAULT_COURSE])


def current_course_key():
    """当前请求的课程 key。"""
    if has_request_context():
        k = getattr(g, 'course_key', None)
        if k:
            return k
    return DEFAULT_COURSE
