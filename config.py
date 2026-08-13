import os

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev'
    
    # 修改数据库路径，确保在可写入的目录中
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # 设置上传文件夹路径
    UPLOAD_FOLDER = os.path.join(basedir, 'app', 'static', 'uploads')
    
    # 确保必要的目录存在
    @staticmethod
    def init_app(app):
        # 确保上传目录存在
        os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
        
        # 确保数据库目录存在
        db_dir = os.path.dirname(app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', ''))
        os.makedirs(db_dir, exist_ok=True)
    
    # 设置允许的文件大小（500MB）
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024
    
    # 允许的视频格式
    ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'webm', 'ogg'}

    # 阿里云 DashScope ASR 配置（语音识别，用于视频 AI 评分）
    # 密钥优先读环境变量 ALIYUN_DASHSCOPE_KEY；其次读 /app/AliyunKey.txt
    ALIYUN_DASHSCOPE_KEY = os.environ.get('ALIYUN_DASHSCOPE_KEY', '')

    # 模板自动重载（docker compose cp 更新文件后无需重启容器）
    TEMPLATES_AUTO_RELOAD = True

    # ── 多课程数据库（每课独立 SQLite）──
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:////app/db/zzlx.db'
    SQLALCHEMY_BINDS = {
        'zzlx': 'sqlite:////app/db/zzlx.db',
        'lllx': 'sqlite:////app/db/lllx.db',
        'gclx': 'sqlite:////app/db/gclx.db',
        'hhlx': 'sqlite:////app/db/hhlx.db',
    }

    # 有意义回复"三重否决"生效起点（YYYY-MM-DD，UTC）。
    # 此日期"之后"创建的作业视频评论才走 观看/时限/语义 三重否决；
    # 此日期"之前"的历史评论保留既有判定，不追溯（彼时无观看追踪数据）。
    # 上线三重否决当天设此值；如需回溯可改小。
    WATCH_TRACKING_START = os.environ.get('WATCH_TRACKING_START', '2026-07-27')