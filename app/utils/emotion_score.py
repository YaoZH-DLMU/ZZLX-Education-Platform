"""
app/utils/emotion_score.py
词云签到情感四维打分引擎（确定性，非 AI）
==========================================
基于 skill.md 的四维语义词典 + 清洗归并 + 公式评分。
AI 仅负责生成 summary/tags，不参与打分。

四维：改革接受度 / 兴趣参与度 / 负担阻抗感 / 自主选择认同度
"""

import re, json, os
from datetime import datetime

# ── 四维语义词典 ──────────────────────────────────────────
# 每个维度：正向词（+1）、负向词（-1）
# 负担维度特殊：正向=负担词（分数升高=负担重）、负向=轻松词（分数降低=负担轻）

DICT = {
    'acceptance': {
        'pos': ['好', '挺好', '有用', '有效', '不错', '喜欢', '认可', '方便', '清晰',
                '自由', '教学相长', '原理加实践', '原理+实践', '真实项目', '学会',
                '收获', '锻炼', '成长', '有意义', '值得'],
        'neg': ['不喜欢', '没用', '不会', '不想', '快逃', '快跑', '别来', '不上课',
                '不用来', '意义不明', '没意义', '浪费时间'],
    },
    'interest': {
        'pos': ['有趣', '新鲜', '活跃', '欢声笑语', '想挑战', '有问有答', '多参加',
                '积极', '讲得开心', '多想一想', '多角度思考', '好玩', '爱学',
                '感兴趣', '开心', '互动', '讨论'],
        'neg': ['无聊', '困', '睡觉', '玩', '少说废话', '不上', '没意思', '厌倦',
                '不想听', '走神'],
    },
    'burden': {  # 正向=负担词（升高=更累），负向=轻松词（降低=更轻松）
        'pos': ['麻烦', '工作量大', '时间紧', '压力大', '焦急不安', '难', '都难',
                '弯曲难', '知识点多而杂', '没时间', '太忙', '不想拍视频', '复杂',
                '困难', '不会', '累', '吃力', '焦头烂额', '头大', '崩溃'],
        'neg': ['轻松', '简单', '方便', '有用', '好复习', '快速复习', '巩固',
                '不难', '容易', '好懂', '清晰'],
    },
    'autonomy': {
        'pos': ['自学', '自律', '靠自己', '自由', '按计划', '合理规划时间', '任务导向',
                '明确任务', '自己推导', '多路径', '主动', '及时复习', '多参加课堂活动',
                '自主学习', '自觉', '独立', '自驱', '自发性'],
        'neg': ['不上课', '不听也能学会', '没有要求', '想上就上不想上就不上',
                '不要小组', '强制', '不会自主学习', '被动', '依赖'],
    },
}

# ── 同义词归并 ────────────────────────────────────────────
SYNONYMS = {
    '好好听课': '认真听课', '认真听讲': '认真听课', '上课一定要听讲': '认真听课',
    '边学边复习': '及时复习', '查漏补缺': '及时复习', '温故知新': '及时复习',
    '自主学习': '自学', '自律': '自学', '靠自己': '自学',
    '工作量大': '工作量大', '时间紧': '工作量大', '压力大': '工作量大',
    '焦急不安': '工作量大',
    '教学相长': '教学相长', '原理加实践': '教学相长', '原理+实践': '教学相长',
    '实践出真知': '教学相长',
}

# ── 噪声词 ────────────────────────────────────────────────
NOISE = {'无', '没有', '不知道', '不知', '额', '嗯', '啊', '哦', '哈', '吧',
         '的', '了', '是', '在', '我', '你', '他', '她', '它', '们'}

DIMENSION_NAMES = {
    'acceptance': '改革接受度',
    'interest': '兴趣参与度',
    'burden': '负担阻抗感',
    'autonomy': '自主选择认同度',
}

DIMENSION_COLORS = {
    'acceptance': '#cc5a43',
    'interest': '#1e7b71',
    'burden': '#d28c1d',
    'autonomy': '#5168a6',
}


def _is_noise(word):
    """判断是否为噪声词"""
    w = word.strip()
    if not w or w in NOISE:
        return True
    if len(w) <= 1:
        return True
    if w.isdigit() and len(w) <= 2:  # 单个/双数字
        return True
    if re.match(r'^[\d\W]+$', w):  # 纯数字+标点
        return True
    return False


def _clean_keyword(word):
    """清洗关键词：去标点、归并同义词"""
    w = re.sub(r'[，。？！、,.?!…·～\-_\s（）()【】\[\]{}"\'""'']+', '', word).strip()
    return SYNONYMS.get(w, w)


def parse_md(md_path):
    """解析签到 md 文件，提取结构化数据。"""
    try:
        text = open(md_path, encoding='utf-8').read()
    except Exception:
        return None

    result = {'question': '', 'export_time': '', 'participants': 0,
              'keywords': [], 'student_ids': []}

    m = re.search(r'\*\*问题：\*\*\s*(.*)', text)
    if m:
        result['question'] = m.group(1).strip()

    m = re.search(r'\*\*导出时间：\*\*\s*(.*)', text)
    if m:
        result['export_time'] = m.group(1).strip()

    m = re.search(r'\*\*参与人数：\*\*\s*(\d+)', text)
    if m:
        result['participants'] = int(m.group(1))

    # 关键词：- word（N次）
    kw_section = re.search(r'## 词云关键词.*?\n(.*?)(?=\n##|\Z)', text, re.S)
    if kw_section:
        for line in kw_section.group(1).strip().split('\n'):
            km = re.match(r'-\s*(.+?)（(\d+)次）', line.strip())
            if km:
                result['keywords'].append({'text': km.group(1).strip(), 'count': int(km.group(2))})

    # 学号：## 参与签到学号 区域
    sid_section = re.search(r'## 参与签到学号.*?\n(.*?)(?=\n##|\Z)', text, re.S)
    if sid_section:
        for line in sid_section.group(1).strip().split('\n'):
            sm = re.match(r'-\s*(\S+)', line.strip())
            if sm:
                result['student_ids'].append(sm.group(1))

    return result


def count_real_students(student_ids):
    """统计真实学生数（学号 8 位以上数字）。测试账号(1-6位)排除。"""
    return sum(1 for sid in student_ids if re.match(r'^\d{8,}$', sid))


def score_signin(keywords, question=''):
    """
    对一次签到的关键词进行四维打分（返回 raw 原始分，不归一化）。
    归一化由 normalize_nodes() 统一处理（相对 max_abs_raw，跨节点可比）。
    """
    # 清洗关键词
    cleaned = []
    for kw in keywords:
        w = _clean_keyword(kw['text'])
        if _is_noise(w):
            continue
        cleaned.append({'text': w, 'count': kw['count']})

    total_valid = sum(kw['count'] for kw in cleaned) or 1

    raw_scores = {}
    evidence = {}
    for dim, dict_words in DICT.items():
        pos_set = set(dict_words['pos'])
        neg_set = set(dict_words['neg'])
        raw = 0
        matched = []
        for kw in cleaned:
            w = kw['text']
            cnt = kw['count']
            if w in pos_set:
                raw += cnt / total_valid
                matched.append(f'+{w}({cnt})')
            elif w in neg_set:
                raw -= cnt / total_valid
                matched.append(f'-{w}({cnt})')
            else:
                # 模糊匹配（词包含词典词或反之）
                for dw in pos_set:
                    if dw in w or w in dw:
                        raw += 0.6 * cnt / total_valid
                        matched.append(f'~+{w}({cnt})')
                        break
                else:
                    for dw in neg_set:
                        if dw in w or w in dw:
                            raw -= 0.6 * cnt / total_valid
                            matched.append(f'~-{w}({cnt})')
                            break

        raw_scores[dim] = round(raw, 4)
        evidence[dim] = matched[:10]

    # 置信度
    valid_kw_count = len(cleaned)
    if valid_kw_count >= 10:
        confidence = 'high'
    elif valid_kw_count >= 5:
        confidence = 'medium'
    else:
        confidence = 'low'

    return {
        'raw': raw_scores,
        'evidence': evidence,
        'confidence': confidence,
        'valid_keyword_count': valid_kw_count,
    }


def normalize_nodes(nodes):
    """对一组节点（含 _raw 字段）做相对归一化：score = clamp(50 + 35 × raw / max_abs_raw, 0, 100)。
    所有节点用同一 max_abs_raw，保证跨节点可比。直接修改 nodes 中的 acceptance/interest/burden/autonomy。"""
    for dim in DICT:
        max_abs = max((abs(n.get('_raw', {}).get(dim, 0)) for n in nodes), default=0)
        if max_abs < 0.001:
            max_abs = 1.0  # 全为0时避免除零
        for n in nodes:
            raw = n.get('_raw', {}).get(dim, 0)
            n[dim] = max(0, min(100, round(50 + 35 * raw / max_abs)))


# ── 课程相关性判定（排除非课程话题如AI讨论/假期收获） ──────
_COURSE_KEYWORDS = {'学习', '课堂', '作业', '教学', '复习', '力学', '翻转', '视频',
                    '答辩', '实验', '考核', '自主', '慕课', '比例', '难点', '要求',
                    '感想', '看法', '课程', '讨论', '听课', '讲课', '建议',
                    '变形', '强度', '校核', '解题', '思考', '理论力学', '材料力学'}


def is_course_related(question):
    """判断签到问题是否与课程教学相关（排除AI讨论/假期等非课程话题）。"""
    q = question or ''
    return any(kw in q for kw in _COURSE_KEYWORDS)


def score_md_file(md_path, prev_scores=None):
    """完整流程：解析 md -> 判定有效+课程相关 -> AI 打分+摘要。返回节点 dict 或 None。"""
    parsed = parse_md(md_path)
    if not parsed:
        return None

    real_count = count_real_students(parsed['student_ids'])
    if real_count < 10:
        return None  # 非有效签到（测试/演示）

    if not is_course_related(parsed['question']):
        return None  # 非课程话题（AI/假期等），不纳入曲线

    # 清洗关键词，拼成 AI 输入
    cleaned = []
    for kw in parsed['keywords']:
        w = _clean_keyword(kw['text'])
        if not _is_noise(w):
            cleaned.append(f'{w}({kw["count"]})')
    keywords_str = ', '.join(cleaned) if cleaned else '（无有效关键词）'

    # AI 打分+摘要（一次调用，含轨迹上下文）
    from app.utils.ai_client import score_emotion
    result = score_emotion(keywords_str, parsed['question'], parsed['participants'], prev_scores)
    if not result:
        return None  # AI 失败，跳过

    node = {
        'date': parsed['export_time'][:10] if parsed['export_time'] else '',
        'title': parsed['question'][:60],
        'participants': parsed['participants'],
        'real_students': real_count,
        'acceptance': result['acceptance'],
        'interest': result['interest'],
        'burden': result['burden'],
        'autonomy': result['autonomy'],
        'summary': result.get('summary', ''),
        'tags': result.get('tags', []),
        'confidence': 'ai_scored',
        'md_file': os.path.basename(md_path),
        'method_version': 'emotion-curve-v2',
    }
    return node
