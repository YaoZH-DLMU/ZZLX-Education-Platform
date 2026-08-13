from flask import Blueprint

api = Blueprint('api', __name__)

# 导入所有API路由
from . import video_api
from . import forum_api
from . import notification_api
from . import user_api
from . import sign_api          # Phase 5-1: 词云签到
from . import ppt_api           # Phase 5-2: PPT课堂
from . import voice_api         # Phase Voice: 语音输入AI处理
from . import graph_api         # Phase 7: 知识/问题图谱
from . import ai_api            # 知知 AI 对话
from . import champion_api      # 打擂台系统
from . import battle_api        # 卡牌对战系统