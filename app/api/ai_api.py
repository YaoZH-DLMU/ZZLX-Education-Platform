"""
app/api/ai_api.py
知知 AI 对话接口 (方案 A：全量知识库注入)
============================================

POST /api/ai/chat
  Body:   { "messages": [ {"role":"user","content":"..."}, ... ] }
  返回:   { "reply": "...", "error": null }
          { "reply": "", "error": "错误信息" }

登录用户可访问（学生/教师均可）。

──────────────────────────────────────────────────────────────
※ 知识库目录：/opt/ZZLXWeb/kb/  （服务器路径，容器内同路径）
   将 .txt / .md 文件放入该目录，无需重启服务，下次请求即生效。
   空目录时 AI 仍可正常对话，但无本地知识库支撑。

──────────────────────────────────────────────────────────────
升级到方案 B（TF-IDF 检索）时只需替换 _load_kb() 函数：
  参见 docs/ai_chat_upgrade_B.md
──────────────────────────────────────────────────────────────
"""

import os
from pathlib import Path
from flask import request, jsonify, g
from flask_login import login_required
from . import api
from app.courses import get_course

# ── 知识库目录（热更新：每次请求重新读取） ────────────────────
_KB_DIR = Path(os.environ.get('KB_DIR', '/opt/ZZLXWeb/kb'))

# 系统提示词模板：{ai_name}/{platform_name}/{ai_persona} 取自当前课程配置，
# {kb_content} 为本地知识库。多课时每门课可有不同 AI 人格。
_SYSTEM_PROMPT_TPL = """你是"{ai_name}"，{platform_name}上的 AI 学习助手。
你的专业领域是{ai_persona}。
回答时请做到：
1. 语言简洁、准确，符合大学力学课程的表述习惯；
2. 遇到公式时用文字或 LaTeX 形式给出；
3. 只在与力学相关时引用本地知识库内容；
4. 若问题超出力学范围，礼貌说明你的专长，不编造答案；
5. 不超过 300 字，如需展开可引导学生追问。

【本地知识库内容】（若为空则无额外资料）：
{kb_content}"""

MAX_HISTORY = 10       # 最多携带的历史轮数（节省 token）
MAX_KB_CHARS = 4000    # 知识库注入上限（方案 A）


def _load_kb() -> str:
    """
    方案 A：读取 KB_DIR 下所有 .txt / .md 文件，拼接为字符串注入系统提示。

    ── 升级到方案 B 时替换此函数 ──
    方案 B 接口约定：
        输入：query (str) — 用户最新一条消息
        输出：相关段落拼接字符串 (str)，格式与方案 A 返回值相同
    参见 docs/ai_chat_upgrade_B.md
    """
    if not _KB_DIR.exists():
        return "（暂无本地知识库）"

    parts = []
    for f in sorted(_KB_DIR.glob("*.txt")) + sorted(_KB_DIR.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                parts.append(f"【{f.stem}】\n{text}")
        except Exception:
            pass

    if not parts:
        return "（暂无本地知识库）"

    combined = "\n\n".join(parts)
    # 超限截断，避免 token 超额
    if len(combined) > MAX_KB_CHARS:
        combined = combined[:MAX_KB_CHARS] + "\n…（内容已截断）"
    return combined


@api.route('/ai/chat', methods=['POST'])
@login_required
def ai_chat():
    """知知 AI 对话接口。"""
    data = request.get_json(silent=True) or {}
    history = data.get('messages', [])

    if not isinstance(history, list) or not history:
        return jsonify({'reply': '', 'error': '消息列表不能为空'}), 400

    # 只保留最近 N 轮，防止 token 膨胀
    history = history[-MAX_HISTORY * 2:]

    kb = _load_kb()
    site = get_course()
    system_content = _SYSTEM_PROMPT_TPL.format(
        ai_name=site['ai_name'],
        platform_name=site['platform_name'],
        ai_persona=site['ai_persona'],
        kb_content=kb,
    )

    messages = [{'role': 'system', 'content': system_content}] + history

    try:
        from app.utils.ai_client import _openai_client
        client = _openai_client()
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=messages,
            max_tokens=600,
            temperature=0.6,
        )
        reply = resp.choices[0].message.content.strip()
        return jsonify({'reply': reply, 'error': None})
    except Exception as e:
        return jsonify({'reply': '', 'error': str(e)}), 500
