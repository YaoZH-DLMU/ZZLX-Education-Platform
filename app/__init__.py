from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_sqlalchemy.session import Session as FSASession
from flask_login import LoginManager
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config

# ── 多课程自定义 Session：按请求的 course_key 选库 ──
class CourseSession(FSASession):
    """每个请求按 g.course_key 选择对应的数据库引擎。
    无请求上下文（启动/迁移）时用默认库（zzlx）。"""
    def get_bind(self, mapper=None, clause=None, **kw):
        from flask import g, has_request_context
        if has_request_context():
            key = getattr(g, 'course_key', None)
            if key:
                # 不检查 key in db.engines（可能因时序问题未初始化），直接取
                engine = db.engines.get(key)
                if engine:
                    return engine
        return db.engine

db = SQLAlchemy(session_options={'class_': CourseSession})
login_manager = LoginManager()
migrate = Migrate()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True

    db.init_app(app)
    migrate.init_app(app, db)

    # 课程选择必须在 Flask-Login 之前注册，确保 user_loader 时 g.course_key 已设
    from app.courses import COURSES as _COURSES, DEFAULT_COURSE as _DEFAULT
    @app.before_request
    def _select_course():
        from flask import g, session, request
        prefix = (request.environ.get('SCRIPT_NAME', '') or '').strip('/').lower()
        # prefix 非空 = 课程前缀请求（/ZZLX/ /LLLX/ 等，nginx 设了 X-Forwarded-Prefix）
        # prefix 空 = 裸路径请求（/api/ /teacher/ 等，nginx 兜底 location 未设 X-Forwarded-Prefix）
        if prefix and prefix in _COURSES:
            # 课程前缀请求：用前缀选库
            g.course_key = prefix
            # 校验 session 课程一致性，不一致则登出（跨课保护）
            from flask_login import current_user
            if current_user.is_authenticated:
                sess_course = session.get('course_key')
                if sess_course and sess_course != prefix:
                    from flask_login import logout_user
                    logout_user()
                    session.clear()
        else:
            # 裸路径请求：用 session 里的 course_key 选库（不触发登出）
            sess_course = session.get('course_key')
            g.course_key = sess_course if sess_course in _COURSES else _DEFAULT

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

    from app.routes import main, auth
    app.register_blueprint(main)
    app.register_blueprint(auth)

    from .api import api as api_blueprint
    app.register_blueprint(api_blueprint, url_prefix='/api')

    # 全局模板变量：当前课程配置 site + 论坛隐藏标志
    @app.context_processor
    def inject_site_config():
        from app.models import SiteConfig
        from app.courses import get_course
        site = get_course()
        try:
            hidden = SiteConfig.get('forum_hidden', '0')
        except Exception:
            hidden = '0'
        return {'site': site, 'course_key': site['key'], 'forum_hidden_flag': hidden}

    # 让 Flask 信任 Nginx 传来的 X-Forwarded-* 头，
    # x_prefix=1 使 url_for() 自动加上 /ZZLX 前缀
    app.wsgi_app = ProxyFix(app.wsgi_app,
                            x_for=1, x_proto=1,
                            x_host=1, x_prefix=1)

    # 幂等补列/建表：对所有课程库执行
    from app.courses import COURSES as _ALL_COURSES
    from sqlalchemy import create_engine as _sa_create_engine
    with app.app_context():
        for _ck in _ALL_COURSES:
            _uri = app.config['SQLALCHEMY_BINDS'].get(_ck) or app.config['SQLALCHEMY_DATABASE_URI']
            _ensure_schema(app, _ck, _sa_create_engine(_uri))

    return app


def _add_column_if_missing(app, table, column, ddl, engine=None):
    """幂等加列：列不存在才 ALTER。"""
    try:
        if engine is None:
            return
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(db.text(f'PRAGMA table_info({table})')).fetchall()]
            if column not in cols:
                conn.execute(db.text(ddl))
                conn.commit()
                app.logger.info('[schema] added %s.%s', table, column)
    except Exception as e:
        app.logger.warning('[schema] add %s.%s skipped: %s', table, column, e)


def _ensure_schema(app, bind_key=None, engine=None):
    """启动时幂等补齐各功能新增的列与表（对指定课程库执行）。"""
    if engine is None:
        app.logger.warning('[schema:%s] no engine, skipped', bind_key)
        return
    try:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(
                db.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]

            # 互动屏蔽
            if 'ppt_interaction' in tables:
                _add_column_if_missing(app, 'ppt_interaction', 'is_blocked',
                    'ALTER TABLE ppt_interaction ADD COLUMN is_blocked BOOLEAN DEFAULT 0', engine)

            # 有意义回复三重否决：检查点 + 观看记录 + 拒绝原因
            if 'video_ai_score' in tables:
                _add_column_if_missing(app, 'video_ai_score', 'checkpoint_sec',
                    'ALTER TABLE video_ai_score ADD COLUMN checkpoint_sec FLOAT', engine)
                _add_column_if_missing(app, 'video_ai_score', 'duration_sec',
                    'ALTER TABLE video_ai_score ADD COLUMN duration_sec FLOAT', engine)
            if 'video_comment' in tables:
                _add_column_if_missing(app, 'video_comment', 'reject_reason',
                    'ALTER TABLE video_comment ADD COLUMN reject_reason VARCHAR(20)', engine)

            # 观看记录表
            if 'video_watch_record' not in tables:
                with engine.begin() as conn:
                    conn.execute(db.text("""
                        CREATE TABLE IF NOT EXISTS video_watch_record (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            video_id INTEGER NOT NULL,
                            user_id INTEGER NOT NULL,
                            intervals_json TEXT DEFAULT '[]',
                            watch_time DATETIME,
                            watch_times_json TEXT DEFAULT '[]',
                            updated_at DATETIME,
                            UNIQUE (video_id, user_id)
                        )"""))
                app.logger.info('[schema:%s] created video_watch_record', bind_key)
            else:
                _add_column_if_missing(app, 'video_watch_record', 'watch_time',
                    'ALTER TABLE video_watch_record ADD COLUMN watch_time DATETIME', engine)
                _add_column_if_missing(app, 'video_watch_record', 'watch_times_json',
                    "ALTER TABLE video_watch_record ADD COLUMN watch_times_json TEXT DEFAULT '[]'", engine)

            # HTML 互动记录表
            if 'html_interaction' not in tables:
                with engine.begin() as conn:
                    conn.execute(db.text("""
                        CREATE TABLE IF NOT EXISTS html_interaction (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            student_id VARCHAR(20) NOT NULL,
                            ppt_session_id INTEGER NOT NULL,
                            slide_path VARCHAR(200) NOT NULL,
                            created_at DATETIME,
                            UNIQUE (student_id, ppt_session_id, slide_path)
                        )"""))
                app.logger.info('[schema:%s] created html_interaction', bind_key)
    except Exception as e:
        app.logger.warning('[schema:%s] ensure skipped: %s', bind_key, e)