"""
app/utils/ai_client.py
DeepSeek AI 客户端封装
  - classify_reply(text) → bool      : 判断回复是否有意义（后台异步调用）
  - summarize_replies(replies, ctx)   : 生成评论区 AI 总结
  - async_classify(app, ...)          : 后台线程入口，调用后自动更新 is_meaningful 字段
"""

from __future__ import annotations
import threading
from pathlib import Path

# ── API Key 读取 ────────────────────────────────────────────────
_api_key: str | None = None

def _get_api_key() -> str:
    global _api_key
    if _api_key is None:
        import os
        # 优先读取环境变量（Docker 部署时使用）
        env_key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
        if env_key:
            _api_key = env_key
        else:
            # 回退到本地文件（开发环境）
            key_file = Path(__file__).parent.parent.parent / 'APIKey.txt'
            _api_key = key_file.read_text(encoding='utf-8').strip()
    return _api_key

def _openai_client():
    from openai import OpenAI
    return OpenAI(api_key=_get_api_key(), base_url='https://api.deepseek.com')


# ── 提示词 ─────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """你是一个严格的教学平台回复质量评审助手。
请判断以下学生的讨论回复是否属于"有意义的回复"。

【无意义的判定标准】（满足任意一条即为无意义）：
1. 纯水内容：单个或少量汉字/字母/数字/表情，如"好""哈哈""666""ok""1""嗯"
2. 泛泛夸奖/评价，无具体内容：如"不错""很好""讲得很好""厉害"
3. 无意义数字或字母组合：如"123""abc""aaa""1234"
4. 纯标点或空白

【有意义的回复】：包含具体的讨论、问题、建议、解析或补充知识点等。

请仅回答 YES（有意义）或 NO（无意义），不要输出任何其他内容。

回复内容：{text}"""

_SUMMARIZE_PROMPT = """{context_line}以下是学生对一段视频的有效评论列表：

{comments}

请对这些评论进行总结，重点提炼：
1. 学生的主要改进建议
2. 学生提出的问题与讨论
3. 对视频内容的核心评价

总结要求：
- 语言简洁，使用中文
- 字数控制在 60-100 字，最长不超过 200 字
- 去除无实质意义的评价，只保留有价值的信息
- 直接给出总结内容，不要加任何前言或结尾"""


# ── 本地快速预判（节省 token） ──────────────────────────────────

def _is_trivially_meaningless(text: str) -> bool:
    """无需调用 API 即可判定为无意义的情况"""
    s = text.strip()
    if not s:
        return True
    if len(s) <= 1:
        return True
    # 纯数字
    if s.isdigit():
        return True
    # 纯 ASCII 字母（3 字符以内），如 abc / ok / hi
    if s.isascii() and s.replace(' ', '').isalpha() and len(s.replace(' ', '')) <= 3:
        return True
    return False


# ── 核心功能 ────────────────────────────────────────────────────

def classify_reply(text: str) -> bool:
    """
    判断单条回复是否有意义。
    返回 True = 有意义，False = 无意义。
    网络/API 异常时默认返回 True（避免误伤正常回复）。
    """
    if _is_trivially_meaningless(text):
        return False

    try:
        client = _openai_client()
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {'role': 'user',
                 'content': _CLASSIFY_PROMPT.format(text=text.strip())}
            ],
            temperature=0,
            max_tokens=5,
        )
        answer = resp.choices[0].message.content.strip().upper()
        return answer.startswith('Y')
    except Exception:
        return True   # 调用失败时保守默认有意义


_CLASSIFY_WITH_TS_PROMPT = """你是教学平台回复质量评审助手。请结合视频转译文本判断学生回复是否有意义。

【一票否决-语义无关】若回复与视频转译文本完全无关联（明显答非所问、随机内容、与视频主题毫不相干），判 NO_SEMANTIC。
【无意义-质量】纯水/泛泛夸奖/无意义数字标点（如"好""哈哈""666""不错""讲得好"），判 NO_QUALITY。
【有意义】包含具体讨论、问题、建议、解析或补充知识点，且与视频内容相关，判 YES。

视频转译文本（节选）：{transcript}

回复内容：{text}

请仅回答 YES / NO_SEMANTIC / NO_QUALITY，不要输出任何其他内容。"""


def classify_reply_with_transcript(text: str, transcript: str):
    """
    结合视频转译文本判断回复是否有意义（三重否决中的语义否决 + 原有质量判定）。
    返回 (is_meaningful: bool, reject_reason: str|None)：
      True, None          -> 有意义
      False, 'semantic'   -> 与视频内容完全无关
      False, 'ai'         -> 质量不达标（水/泛夸/无意义）
    API 异常时保守默认 (True, None)。
    """
    if _is_trivially_meaningless(text):
        return False, 'ai'
    ts = (transcript or '')[:2000]
    try:
        client = _openai_client()
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {'role': 'user',
                 'content': _CLASSIFY_WITH_TS_PROMPT.format(text=text.strip(), transcript=ts)}
            ],
            temperature=0,
            max_tokens=10,
        )
        answer = resp.choices[0].message.content.strip().upper()
        if answer.startswith('YES'):
            return True, None
        if 'SEMANTIC' in answer:
            return False, 'semantic'
        return False, 'ai'
    except Exception:
        return True, None   # 调用失败保守默认有意义


_EMOTION_SCORE_PROMPT = """你是教学改革情感分析助手。根据一次词云签到的关键词数据，从四个维度评估学生的情感状态。

【背景】这是一门采用PBL（项目式学习）/翻转课堂教学改革的大学课程。签到数据反映学生对教学改革的态度变化轨迹。整体趋势应该是：改革初期学生观望/抵触->体验后逐渐接受->后期主动参与。负担感初期上升（任务量增加）->中期达峰->后期回落（适应后不再觉得累）。

【四个维度】（均0-100整数）
1. acceptance 改革接受度：学生对教学改革模式的认同。高=积极接受"有用""收获""教学相长"；低=抵触"没用""不想""快逃"。
2. interest 兴趣参与度：学习兴趣和参与积极性。高="有趣""新鲜""积极""讨论"；低="无聊""困""睡觉"。
3. burden 负担阻抗感：学习负担和阻力强度。高="难""麻烦""工作量大""压力大""累"；低="轻松""简单""不难"。注意此维度高=负担重，不是越好。
4. autonomy 自主选择认同度：自主学习和自我驱动的认同。高="自学""自律""自己规划""主动""自觉"；低="被动""依赖""强制""不会自主"。

【评分校准示例】（重要！请参考这些逻辑打分）
- 学生偏好传统作业模式 -> acceptance低(30-40, 不认同改革)、burden也低(25-35, 传统模式是舒适区不觉得累)。不是"偏好传统=高负担"。
- 学生说"难但有收获""工作量大但收获不少" -> acceptance高(65-75, 认可价值)、burden也高(65-75, 确实累)、interest中高(55-65, 有挑战感)。"难"和"收获"要分开计分。
- 学生说"不上课""自学""躺着上课"（在"理想课堂"话题下） -> autonomy高(75-85, 自主向往)、interest中高(60-70, 幽默参与)、burden低(30-40, 追求轻松)。不是消极。
- 期末复习期间学生要"题库""重点""范围" -> acceptance中(45-55, 正常应考需求不等于拒绝改革)、burden中高(60-70, 考试压力)、autonomy中低(40-50, 支架需求但非完全被动)。不应大幅下降。
- 学生"不用慕课""不利用视频"但用其他方式学习 -> acceptance中(55-65, 偏好其他方式不等于拒绝改革)、autonomy中(50-60, 有自学意愿只是不用这个平台)。不应判极低。
- 学生给学弟学妹"好好学习""认真听课"的建议 -> acceptance高(70-80, 以过来人身份认可课程)、autonomy高(75-85, 主动传递经验=角色转变)。
- 学生讨论"难点""弯曲难""公式多" -> burden高(70-80, 确实难)、但acceptance中(55-65, 主动讨论难点=仍在参与)、interest中(50-60, 认知投入)。

【评分要点】
- 必须结合签到问题的语境理解关键词。
- "难"不一定是负面--讨论难点本身说明学生在积极参与。
- 关注高频词的组合关系，而非单个词的字面。
- 合理使用分数范围，有明显正/负信号时给出高于65或低于40的分数。
{trajectory}

【签到数据】
问题：{question}
参与人数：{participants}
关键词（按频次降序）：{keywords}

请返回JSON：
{{"acceptance": 整数, "interest": 整数, "burden": 整数, "autonomy": 整数, "summary": "2-3句话分析", "tags": ["1-2个标签"]}}
只返回JSON，不要其他内容。"""


def score_emotion(keywords_str, question, participants, prev_scores=None):
    """AI 一次性打分+摘要。prev_scores 为上一次签到的分数（轨迹连续性）。返回 dict 或 None。"""
    try:
        client = _openai_client()
        if prev_scores:
            trajectory = f"\n【轨迹上下文】上一次签到分数：改革接受度={prev_scores.get('acceptance',50)}，兴趣参与度={prev_scores.get('interest',50)}，负担阻抗感={prev_scores.get('burden',50)}，自主选择认同度={prev_scores.get('autonomy',50)}。请考虑情感变化的连续性：分数应在上次基础上合理波动（±15以内为正常微调），避免单次剧烈跳变（±30以上），除非关键词证据确实支持大幅变化。"
        else:
            trajectory = ""
        prompt = _EMOTION_SCORE_PROMPT.format(
            question=question[:200],
            participants=participants,
            keywords=keywords_str[:1000],
            trajectory=trajectory,
        )
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        import json
        text = resp.choices[0].message.content.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        data = json.loads(text)
        # 校验
        for d in ['acceptance', 'interest', 'burden', 'autonomy']:
            data[d] = max(0, min(100, int(data.get(d, 50))))
        data.setdefault('summary', '')
        data.setdefault('tags', [])
        return data
    except Exception:
        return None


_BATCH_PROMPT = """你是教学改革情感分析助手。以下是一门PBL/翻转课堂教学改革课程的全部词云签到数据。请从四个维度为每次签到打分(0-100)，并生成摘要。

【背景】这是一门采用PBL教学改革的大学课程。签到数据反映学生对教学改革的态度变化轨迹。整体趋势：改革初期学生观望/抵触->体验后逐渐接受->后期主动参与。负担感初期上升->中期达峰->后期回落。你能看到全部签到数据，请从全局视角打分，保证趋势连贯。

【四个维度】（均0-100整数）
1. acceptance 改革接受度：学生对教学改革模式的认同。高=积极接受"有用""收获""教学相长"；低=抵触"没用""不想""快逃"。
2. interest 兴趣参与度：学习兴趣和参与积极性。高="有趣""新鲜""积极""讨论"；低="无聊""困""睡觉"。
3. burden 负担阻抗感：学生对学习负担的**抗拒/阻抗程度**，不是单纯负担量。高=不仅累且抗拒"麻烦""不如纸质""不想拍视频""受不了"；中=累但接受"累但有收获""难但有意思"（有负担无阻抗）；低="轻松""简单""不难"。关键区分："累"+"收获"并存时给中(50-60)而非高(70+)。
4. autonomy 自主选择认同度：自主学习和自我驱动的认同。高="自学""自律""自己规划""主动""自觉"；低="被动""依赖""强制"。

【评分校准示例】
- 偏好传统作业模式 -> acceptance低(30-40)、burden也低(25-35, 舒适区不觉得累)。
- "难但有收获" -> acceptance高(65-75)、burden也高(65-75)。两者独立计分。
- "不上课""自学"在"理想课堂"话题 -> autonomy高(75-85, 幽默表达自主向往)。
- 期末要"题库""重点" -> acceptance中(45-55, 正常应考不等于拒绝改革)、burden中高(60-70)。
- "不用慕课"但用其他方式 -> acceptance中(55-65, 偏好其他方式不等于拒绝)。
- 给学弟学妹建议"好好学习" -> acceptance高(70-80)、autonomy高(75-85, 角色转变)。
- 讨论"难点""弯曲难" -> burden高(70-80)、acceptance中(55-65, 主动讨论=仍在参与)。
- 翻转课堂首次体验后"累"+"受益匪浅"+"挺有意思"并存 -> 关键拐点：acceptance跳升(65-75)、burden下降(50-55, 累但不是阻抗)、interest上升(60-70)、autonomy上升(55-65)。四条曲线同步转正。
- "理想课堂"话题中学生描绘"不上课""轻松" -> 说明对现状不满=当前burden偏高(60-65)；幽默表达=engaged=interest中高(60-65)；自主向往=autonomy高(70-80)。
- "实践是检验真理的唯一标准""教学相长" -> 学生用工程视角思考=autonomy高(65-75)、acceptance高(65-75)。
- "简单点""时间多一点""批评别太狠"（答辩/汇报话题）-> 调整诉求非阻抗，burden中(50-55)，acceptance中(60-68)。
- 期末复习"题库""重点""考简单点" -> 应考需求有负担但非抗拒，burden中高(55-60)，acceptance中(48-55)。
- 阻抗感底线：中后期（5月以后）即使无抱怨词，学生仍在challenging课程中，burden不应低于40。无阻抗词时保持45-55，不要骤降到30-35。

【评分要点】
- 结合每次签到的问题语境理解关键词。
- 你能看到全部签到，请确保分数趋势连贯：接受度/兴趣/自主度初期低->中期回升->后期高；阻抗感初期高(60-75)->波动中下降->后期低(35-50)。阻抗感的波动是正常的（难点阶段会回升），但整体趋势应向下。
- 早期签到的分数可以根据后续变化适当校准。
- 合理使用分数范围（30-85），有明显信号时给高分或低分。

【全部签到数据】
{signins_data}

请返回JSON数组，每个元素对应一次签到（按上述顺序）：
[{{"acceptance": 整数, "interest": 整数, "burden": 整数, "autonomy": 整数, "summary": "2-3句话", "tags": ["标签"]}}, ...]
只返回JSON数组，不要其他内容。"""


def score_emotion_batch(signins):
    """一次性为全部签到打分+摘要（全局视角，趋势连贯）。返回 list[dict] 或 None。"""
    try:
        client = _openai_client()
        parts = []
        for i, si in enumerate(signins):
            kws = ', '.join(f'{kw["text"]}({kw["count"]})' for kw in si['keywords'][:30])
            parts.append(f'签到{i+1} ({si["date"]}, {si["participants"]}人)\n问题：{si["question"][:100]}\n关键词：{kws}')
        signins_data = '\n\n'.join(parts)

        prompt = _BATCH_PROMPT.format(signins_data=signins_data)
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.3,
            max_tokens=4000,
        )
        import json
        text = resp.choices[0].message.content.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        data = json.loads(text)
        if not isinstance(data, list):
            data = [data]
        for item in data:
            for d in ['acceptance', 'interest', 'burden', 'autonomy']:
                item[d] = max(0, min(100, int(item.get(d, 50))))
            item.setdefault('summary', '')
            item.setdefault('tags', [])
        return data
    except Exception as e:
        print(f'[emotion] batch score error: {e}')
        return None


_EMOTION_SUMMARY_PROMPT = """你是教学分析助手。根据一次词云签到数据，生成简短的情感分析摘要。

签到问题：{question}
参与人数：{participants}
四维分数（0-100）：改革接受度={acceptance}，兴趣参与度={interest}，负担阻抗感={burden}（越高=负担越重），自主选择认同度={autonomy}
命中关键词证据：{evidence}

请返回 JSON：{{"summary": "2-3句话分析摘要，描述学生情感状态和变化趋势", "tags": ["1-2个阶段标签"]}}
只返回 JSON，不要其他内容。"""


def generate_emotion_summary(node):
    """为情感曲线节点生成 AI 摘要+标签。返回 (summary, tags)。"""
    try:
        client = _openai_client()
        prompt = _EMOTION_SUMMARY_PROMPT.format(
            question=node.get('title', ''),
            participants=node.get('participants', 0),
            acceptance=node.get('acceptance', 50),
            interest=node.get('interest', 50),
            burden=node.get('burden', 50),
            autonomy=node.get('autonomy', 50),
            evidence=str(node.get('evidence', {}))[:500],
        )
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        import json
        text = resp.choices[0].message.content.strip()
        # 去除可能的 ```json 包裹
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        data = json.loads(text)
        return data.get('summary', ''), data.get('tags', [])
    except Exception:
        return '', []


def summarize_replies(replies: list[str], context: str = '') -> str:
    """
    生成一批评论的 AI 总结。
    replies : 纯文本字符串列表（仅传入有意义的回复）
    context : 视频标题/题目描述等背景（可选）
    """
    if not replies:
        return '暂无有效评论可总结。'

    context_line = f'视频背景：{context}\n\n' if context else ''
    comments_text = '\n'.join(f'- {r}' for r in replies)

    try:
        client = _openai_client()
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {'role': 'user',
                 'content': _SUMMARIZE_PROMPT.format(
                     context_line=context_line,
                     comments=comments_text
                 )}
            ],
            temperature=0.3,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f'AI 总结生成失败：{e}'


# ── 后台异步分类 ────────────────────────────────────────────────

def _classify_and_save(app, model_name: str, record_id: int, text: str) -> None:
    """在 Flask app context 内执行判断，并将结果写回数据库。"""
    result = classify_reply(text)
    with app.app_context():
        from app import db
        from app.models import VideoComment, Reply
        _model_map = {'VideoComment': VideoComment, 'Reply': Reply}
        Model = _model_map.get(model_name)
        if Model:
            record = db.session.get(Model, record_id)
            if record is not None:
                record.is_meaningful = result
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()


def async_classify(app, model_name: str, record_id: int, text: str) -> None:
    """
    启动守护线程异步执行 is_meaningful 判断。
    用法（在提交路由中）::

        from app.utils.ai_client import async_classify
        from flask import current_app
        async_classify(current_app._get_current_object(),
                       'VideoComment', comment.id, comment.content)
    """
    t = threading.Thread(
        target=_classify_and_save,
        args=(app, model_name, record_id, text),
        daemon=True,
    )
    t.start()


# ── 语音转录文本结构化处理 ──────────────────────────────────────

_VOICE_PROMPTS = {
    'sign_question': (
        '你是课堂助手。将以下语音识别文本整理为一句简洁的签到问题（不超过40字）。'
        '去除"请签到"及其后的内容；同时去除文末因停顿产生的残留指令片段（如"请""请签""请签到"等）。'
        '只输出整理后的问题，不要任何解释。\n\n输入：{text}'
    ),
    'choice': (
        '你是课堂助手。从以下语音识别文本中提取选择题内容，以JSON输出。\n'
        '格式：{{"question":"题目（不超过60字，去掉请选择等结束语）","options":["选项A","选项B","选项C"]}}\n'
        '要求：\n'
        '1. options数组恰好3项，去掉A/B/C等字母前缀，只保留选项文字。\n'
        '2. 如果某个选项文字仅为"选择""请选择""选""择"等录音停止指令词而非实际答案，用空字符串替代该项。\n'
        '3. 如果文末出现多余的"请选择"短语（因重复说出），只保留题目本身内容。\n'
        '只输出JSON，不要任何解释。\n\n输入：{text}'
    ),
    'vote': (
        '你是课堂助手。从以下语音识别文本中提取投票题内容，以JSON输出。\n'
        '格式：{{"question":"题目（不超过60字，去掉请投票等结束语）","label1":"蓝方标签（不超过12字）","label2":"红方标签（不超过12字）"}}\n'
        '蓝方通常是支持/正方一侧，红方是反对/负方一侧。\n'
        '如果文末出现多余的"请投票"短语（因重复说出），只保留题目本身内容。\n'
        '只输出JSON，不要任何解释。\n\n输入：{text}'
    ),
    'short': (
        '你是课堂助手。将以下语音识别文本整理为一个清晰的简答题问题（不超过60字）。'
        '去除"请回答"及其后的内容；同时去除文末因停顿产生的残留指令片段（如"请""请回""请回答"等）。'
        '只输出整理后的问题，不要任何解释。\n\n输入：{text}'
    ),
    'video_comment': (
        '你是大学材料力学课程助手。'
        '以下是学生对材料力学作业视频录制的语音评论原文（通过语音转文字识别），'
        '请将其整理为简洁通顺的中文评论（不超过100字），'
        '纠正明显的语音识别错误和错别字，保留学生的原意和专业术语。'
        '只输出整理后的评论内容，不要任何前言或解释。\n\n输入：{text}'
    ),
}


def process_voice_input(text: str, vtype: str) -> dict:
    """
    处理语音转录文本，返回结构化数据。
    vtype: 'sign_question' | 'choice' | 'vote' | 'short'
    返回:
      sign_question / short → {'question': str, 'error': None}
      choice  → {'question': str, 'options': [A, B, C], 'error': None}
      vote    → {'question': str, 'label1': str, 'label2': str, 'error': None}
    发生错误时 error 字段非空。
    使用标准库 urllib 直接调用 DeepSeek HTTP 接口，无需安装额外依赖。
    """
    prompt_tpl = _VOICE_PROMPTS.get(vtype)
    if not prompt_tpl:
        return {'error': f'未知类型: {vtype}'}
    if not text.strip():
        return {'error': '输入文本为空'}

    try:
        import json as _json
        import urllib.request as _urllib
        import re as _re

        api_key = _get_api_key()
        payload = {
            'model': 'deepseek-chat',
            'messages': [{'role': 'user', 'content': prompt_tpl.format(text=text.strip())}],
            'temperature': 0.1,
            'max_tokens': 300,
        }
        req = _urllib.Request(
            'https://api.deepseek.com/chat/completions',
            data=_json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with _urllib.urlopen(req, timeout=30) as resp:
            body = _json.loads(resp.read().decode('utf-8'))
        raw = body['choices'][0]['message']['content'].strip()
    except Exception as e:
        return {'error': f'AI接口调用失败: {type(e).__name__}: {e}'}

    if vtype in ('sign_question', 'short', 'video_comment'):
        return {'question': raw[:200], 'error': None}

    # choice / vote → 解析 JSON
    try:
        import json as _json
        import re as _re
        s = raw
        # 先去掉 ```json ... ``` 包裹
        if '```' in s:
            s = s.split('```')[1].lstrip('json').strip()
        # 用正则提取第一个 JSON 对象（山AI输出前后带说明文字）
        m = _re.search(r'\{.*\}', s, _re.DOTALL)
        if m:
            s = m.group(0)
        data = _json.loads(s)
    except Exception:
        # 解析失败时把原始文本作为 question 回退
        return {'question': raw[:200], 'options': [], 'label1': '', 'label2': '', 'error': 'JSON解析失败，请手动修改'}

    if vtype == 'choice':
        opts = data.get('options', [])
        while len(opts) < 3:
            opts.append('')
        return {'question': str(data.get('question', ''))[:200], 'options': opts[:3], 'error': None}

    if vtype == 'vote':
        return {
            'question': str(data.get('question', ''))[:200],
            'label1':   str(data.get('label1', '蓝方'))[:30],
            'label2':   str(data.get('label2', '红方'))[:30],
            'error':    None,
        }

    return {'error': '未知类型'}
