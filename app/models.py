import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from sqlalchemy import func
from flask_login import current_user

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))

    # 第2阶段新增字段
    is_teacher       = db.Column(db.Boolean, default=False)          # 教师账号标志
    avatar           = db.Column(db.String(200), nullable=True)       # 头像文件路径
    profile_bg_color = db.Column(db.String(20), default='#ffffff')   # 个人主页背景色
    bio              = db.Column(db.String(200), nullable=True)       # 个人简介
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    reward_points    = db.Column(db.Integer, default=0)               # 积分
    meaningful_replies_count = db.Column(db.Integer, default=0)       # 有意义回复数（结算更新）

    # 关系
    videos    = db.relationship('Video',        backref='author',   lazy=True)
    comments  = db.relationship('VideoComment', backref='author',   lazy=True)
    favorites = db.relationship('VideoFavorite',backref='user',     lazy=True)
    ratings   = db.relationship('VideoRating',  backref='user',     lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_adv_test(self):
        """高级测试账号：学号 202601-202610，可浏览教师页面但不能写入。"""
        try:
            n = int(self.student_id)
            return 202601 <= n <= 202610
        except (ValueError, TypeError):
            return False

    @property
    def avatar_url(self):
        """返回头像 URL，无头像时使用默认占位图"""
        if self.avatar:
            return f'/static/uploads/avatars/{self.avatar}'
        return '/static/images/default_avatar.png'

class Video(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    url = db.Column(db.String(200), nullable=False)
    thumbnail = db.Column(db.String(200))

    # 视频分类字段
    type = db.Column(db.String(20))        # 'homework'(作业视频) | 'flipped'(翻转课堂)
    subject = db.Column(db.String(20))     # '理论力学' | '材料力学'
    chapter = db.Column(db.Integer)        # 章节号 1-12（作业视频）
    problem_no = db.Column(db.Integer)     # 题号 1-30（作业视频）
    group_no = db.Column(db.Integer)       # 小组编号 1-15（翻转课堂）
    class_no = db.Column(db.Integer)       # 翻转课堂次数 1-7
    is_featured = db.Column(db.Boolean, default=False)  # 是否为精品视频

    views = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    # 添加关系
    comments = db.relationship('VideoComment', backref='video', lazy=True, 
                             order_by='desc(VideoComment.created_at)')
    favorites = db.relationship('VideoFavorite', backref='video', lazy=True)
    ratings = db.relationship('VideoRating', backref='video', lazy=True)

    @property
    def average_rating(self):
        """加权综合评分 = (学生分总和 + 教师均分×w2 + AI分×w3) / (学生人数 + w2 + w3)"""
        teacher_ids_sub = db.session.query(User.id).filter(User.is_teacher == True).subquery()

        stu = db.session.query(
            func.sum(VideoRating.value), func.count(VideoRating.id)
        ).filter(
            VideoRating.video_id == self.id,
            VideoRating.user_id.notin_(teacher_ids_sub)
        ).first()
        student_sum   = stu[0] or 0
        student_count = stu[1] or 0

        teacher_avg_raw = db.session.query(func.avg(VideoRating.value)).filter(
            VideoRating.video_id == self.id,
            VideoRating.user_id.in_(teacher_ids_sub)
        ).scalar()
        teacher_avg = float(teacher_avg_raw) if teacher_avg_raw else None

        # AI 评分（VideoAIScore 定义在后面，运行时已加载）
        ai_obj   = VideoAIScore.query.filter_by(video_id=self.id).first()
        ai_score = ai_obj.score if ai_obj else None

        try:
            cfg       = StarConfig.get_config()
            w_teacher = cfg.teacher_weight or 3
            w_ai      = cfg.ai_weight or 2
        except Exception:
            w_teacher, w_ai = 3, 2

        total_score  = float(student_sum)
        total_weight = float(student_count)
        if teacher_avg is not None:
            total_score  += teacher_avg * w_teacher
            total_weight += w_teacher
        if ai_score is not None:
            total_score  += ai_score * w_ai
            total_weight += w_ai

        return round(total_score / total_weight, 1) if total_weight > 0 else 0.0

    @property
    def has_transcript(self):
        """是否已 AI 转译成功（有 VideoAIScore 且存了转写文本）。用于卡片着色/图章。"""
        ai = VideoAIScore.query.filter_by(video_id=self.id).first()
        return bool(ai and ai.transcript)

    @property
    def user_rating(self):
        """获取当前用户的评分"""
        if not current_user.is_authenticated:
            return None
        rating = VideoRating.query.filter_by(
            user_id=current_user.id,
            video_id=self.id
        ).first()
        return rating.value if rating else None

    @property
    def is_favorited(self):
        """检查当前用户是否收藏了该视频"""
        if not current_user.is_authenticated:
            return False
        return VideoFavorite.query.filter_by(
            user_id=current_user.id,
            video_id=self.id
        ).first() is not None

    @property
    def favorites_count(self):
        return VideoFavorite.query.filter_by(video_id=self.id).count()

    @property
    def comments_count(self):
        return VideoComment.query.filter_by(video_id=self.id).count()

    @property
    def visible_comments(self):
        """返回对当前用户可见的评论：
        - 被屏蔽评论仅教师可见
        - 测试账号(学号1-50)的评论仅本人和教师可见。"""
        from sqlalchemy import or_ as _or, and_ as _and
        _TEST_IDS = [str(i) for i in range(1, 51)]
        test_q = db.session.query(User.id).filter(User.student_id.in_(_TEST_IDS))
        q = VideoComment.query.filter_by(video_id=self.id).order_by(VideoComment.created_at.desc())
        if current_user.is_authenticated and current_user.is_teacher:
            return q.all()
        # 非教师：过滤掉被屏蔽的评论
        q = q.filter_by(is_hidden=False)
        if current_user.is_authenticated:
            return q.filter(_or(VideoComment.user_id.notin_(test_q),
                                VideoComment.user_id == current_user.id)).all()
        return q.filter(VideoComment.user_id.notin_(test_q)).all()

# 视频评论
class VideoComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    # Phase 6：AI 质量评估（True=有意义，默认 True 以兼容历史数据）
    is_meaningful = db.Column(db.Boolean, default=True, server_default='1')
    # 教师屏蔽评论（仅教师和管理员可见）
    is_hidden = db.Column(db.Boolean, default=False, server_default='0')
    # 结算三重否决的拒绝原因：watch(未看检查点)/temporal(超10分钟)/semantic(语义无关)/short(过短)/none
    reject_reason = db.Column(db.String(20), nullable=True)

# 视频收藏
class VideoFavorite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)

class VideoRating(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    video_id = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'video_id', name='unique_user_video_rating'),
    )


class VideoAIScore(db.Model):
    """AI 自动评分：语音转写 → 知识点匹配 → DeepSeek 评分（7-9 分）"""
    __tablename__ = 'video_ai_score'

    id         = db.Column(db.Integer, primary_key=True)
    video_id   = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False, unique=True)
    score      = db.Column(db.Float,   nullable=False)   # 7.0 - 9.0
    transcript = db.Column(db.Text,    nullable=True)    # ASR 转写文本
    kp_matched = db.Column(db.Text,    nullable=True)    # 匹配到的知识点 JSON
    reason     = db.Column(db.Text,    nullable=True)    # DeepSeek 评分理由
    checkpoint_sec = db.Column(db.Float, nullable=True)  # 有意义回复校验：随机检查点（视频时长10%-90%）
    duration_sec   = db.Column(db.Float, nullable=True)  # 视频时长（秒）
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VideoWatchRecord(db.Model):
    """学生观看视频的 1x 区间记录（用于检查点观看校验）。
    intervals_json: [[start,end],...] 合并后的 1x 速率实际观看区间（秒）。
    每学生每视频一条；结算时后端判断 [cp-5,cp+5] 是否被覆盖。"""
    __tablename__ = 'video_watch_record'
    id            = db.Column(db.Integer, primary_key=True)
    video_id      = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    user_id       = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    intervals_json = db.Column(db.Text, default='[]')
    watch_time    = db.Column(db.DateTime, nullable=True)   # 最近一次覆盖检查点的时刻（展示用）
    watch_times_json = db.Column(db.Text, default='[]')     # 历次覆盖检查点的时刻列表（时限判定：评论与它之前最近一次观看比）
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('video_id', 'user_id', name='uq_video_watch_user'),)

# 在现有模型之后添加论坛相关模型

class ForumBoard(db.Model):
    __tablename__ = 'forum_boards'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # 板块名称
    description = db.Column(db.String(200))  # 板块描述
    posts = db.relationship('Post', backref='board', lazy='dynamic')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Post(db.Model):
    __tablename__ = 'forum_posts'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    # HTML 嵌入内容（板块1专用，以 JSON 字符串存储：{"code": "..."}）
    html_code = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    views = db.Column(db.Integer, default=0)  # 浏览次数
    like_count = db.Column(db.Integer, default=0)  # 点赞数（冗余缓存）

    board_id = db.Column(db.Integer, db.ForeignKey('forum_boards.id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author = db.relationship('User', backref='posts')

    replies = db.relationship('Reply', backref='post', lazy='dynamic')
    images = db.relationship('PostImage', backref='post', lazy='dynamic')


class PostLike(db.Model):
    """帖子点赞记录（一人一帖一次）"""
    __tablename__ = 'post_likes'
    id = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    post_id  = db.Column(db.Integer, db.ForeignKey('forum_posts.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('user_id', 'post_id'),)

class Reply(db.Model):
    __tablename__ = 'forum_replies'
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    post_id = db.Column(db.Integer, db.ForeignKey('forum_posts.id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author = db.relationship('User', backref='replies')
    # Phase 6：AI 质量评估（True=有意义，默认 True 以兼容历史数据）
    is_meaningful = db.Column(db.Boolean, default=True, server_default='1')
    
    images = db.relationship('ReplyImage', backref='reply', lazy='dynamic')

class PostImage(db.Model):
    __tablename__ = 'forum_post_images'
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    post_id = db.Column(db.Integer, db.ForeignKey('forum_posts.id'), nullable=False)

    @property
    def display_name(self):
        parts = urlsplit(self.url or '')
        query_name = parse_qs(parts.query).get('display_name', [''])[0]
        if query_name:
            return unquote(query_name)
        filename = os.path.basename(parts.path)
        filename = re.sub(r'^\d+_[0-9a-f]{8}_', '', filename, flags=re.IGNORECASE)
        filename = re.sub(r'^\d+_', '', filename)
        return filename or '附件'

class ReplyImage(db.Model):
    __tablename__ = 'forum_reply_images'
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reply_id = db.Column(db.Integer, db.ForeignKey('forum_replies.id'), nullable=False)

    @property
    def display_name(self):
        parts = urlsplit(self.url or '')
        query_name = parse_qs(parts.query).get('display_name', [''])[0]
        if query_name:
            return unquote(query_name)
        filename = os.path.basename(parts.path)
        filename = re.sub(r'^\d+_[0-9a-f]{8}_', '', filename, flags=re.IGNORECASE)
        filename = re.sub(r'^\d+_', '', filename)
        return filename or '附件'


# ─── 通知系统 ───────────────────────────────────────────────

class Notification(db.Model):
    """
    系统通知模型。
    type 枚举值：
      video_replaced   - 你评价过的视频已被替换，请重新评分
      new_comment      - 你的视频收到新评论
      new_rating       - 你的视频收到新评分
      new_favorite     - 你的视频收到新收藏
      system           - 系统公告
    """
    __tablename__ = 'notifications'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)   # 接收者
    sender_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)    # 触发者（可空）
    type       = db.Column(db.String(30), nullable=False, default='system')
    title      = db.Column(db.String(100), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    link       = db.Column(db.String(200), nullable=True)   # 点击跳转地址
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user   = db.relationship('User', foreign_keys=[user_id],   backref='notifications')
    sender = db.relationship('User', foreign_keys=[sender_id])

    def to_dict(self):
        """序列化为 API 响应格式"""
        return {
            'id':         self.id,
            'type':       self.type,
            'title':      self.title,
            'content':    self.content,
            'link':       self.link,
            'is_read':    self.is_read,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M'),
            'sender':     self.sender.username if self.sender else None,
        }


# ─── 第4阶段：个人成就系统 ──────────────────────────────────────

class StarConfig(db.Model):
    """
    教师可配置的第4、5星解锁条件（全局唯一行，id=1）。
    第1-3星的条件固定不变，第4-5星由教师在后台调整。
    """
    __tablename__ = 'star_config'

    id               = db.Column(db.Integer, primary_key=True)
    # 第4星
    s4_min_views     = db.Column(db.Integer, default=20)   # 视频总观看次数
    s4_min_favorites = db.Column(db.Integer, default=15)   # 视频被收藏总次数
    # 第5星
    s5_min_videos         = db.Column(db.Integer, default=3)    # 发布视频数量
    s5_min_avg_rating     = db.Column(db.Float,   default=9.0)  # 所有视频平均评分
    s5_min_video_comments = db.Column(db.Integer, default=20)   # 有效视频评论数（AI审核为有意义）
    # AI 评分权重（等效学生数，用于加权综合评分）
    teacher_weight = db.Column(db.Integer, default=3)  # 教师评分等效学生数（默认3）
    ai_weight      = db.Column(db.Integer, default=2)  # AI 评分等效学生数（默认2）
    # 有意义回复配置
    meaningful_threshold = db.Column(db.Integer, default=10)  # 有意义回复数门槛（默认10）
    meaningful_weight    = db.Column(db.Float,   default=0.3)  # 每条超额回复的分数权重（默认0.3）
    # 积分兑换系数（X 积分 = 1 分过程化考核成绩）
    point_exchange_rate = db.Column(db.Integer, default=10)  # 默认10积分兑换1分
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    @classmethod
    def get_config(cls):
        """获取全局配置（自动初始化默认值）"""
        cfg = cls.query.first()
        if not cfg:
            cfg = cls()
            db.session.add(cfg)
            db.session.commit()
        return cfg


def compute_stars(user):
    """
    计算用户当前点亮的星数 (0-5)。

    第1星：已上传视频 & 已评分 & 已评论 & 已收藏 & 已回帖（论坛回复）
    第2星：第1星 + 累计评分次数 >= 10
    第3星：第2星 + 有意义视频评论次数 >= 10
    第4星：第3星 + 所发视频总观看次数 / 总收藏次数达到教师配置阈值
    第5星：第4星 + 发布视频数 / 平均评分 / 有效回复数达到教师配置阈值
    """
    from sqlalchemy import func as sqlfunc

    cfg = StarConfig.get_config()

    # ── Star 1 ──
    has_video    = Video.query.filter_by(user_id=user.id).count() > 0
    has_rating   = VideoRating.query.filter_by(user_id=user.id).count() > 0
    has_comment  = VideoComment.query.filter_by(user_id=user.id).count() > 0
    has_favorite = VideoFavorite.query.filter_by(user_id=user.id).count() > 0
    has_reply    = Reply.query.filter_by(author_id=user.id).count() > 0
    s1 = all([has_video, has_rating, has_comment, has_favorite, has_reply])

    # ── Star 2 ──
    rating_count = VideoRating.query.filter_by(user_id=user.id).count()
    s2 = s1 and rating_count >= 10

    # ── Star 3 ──（只统计 AI 判定为有意义的视频评论）
    vcm_count = VideoComment.query.filter_by(
        user_id=user.id, is_meaningful=True
    ).count()
    s3 = s2 and vcm_count >= 10

    # ── 有效视频评论数（★5 用）──
    vc_meaningful_count = VideoComment.query.filter_by(
        user_id=user.id, is_meaningful=True
    ).count()

    # ── Star 4 ──
    total_views = db.session.query(sqlfunc.sum(Video.views))\
        .filter(Video.user_id == user.id).scalar() or 0
    total_favs  = db.session.query(sqlfunc.count(VideoFavorite.id))\
        .join(Video, VideoFavorite.video_id == Video.id)\
        .filter(Video.user_id == user.id).scalar() or 0
    s4 = s3 and int(total_views) >= cfg.s4_min_views and int(total_favs) >= cfg.s4_min_favorites

    # ── Star 5 ──
    video_count    = Video.query.filter_by(user_id=user.id).count()
    avg_rating_raw = db.session.query(sqlfunc.avg(VideoRating.value))\
        .join(Video, VideoRating.video_id == Video.id)\
        .filter(Video.user_id == user.id).scalar()
    avg_rating = float(avg_rating_raw) if avg_rating_raw else 0.0
    s5 = (s4
          and video_count          >= cfg.s5_min_videos
          and avg_rating           >= cfg.s5_min_avg_rating
          and vc_meaningful_count  >= cfg.s5_min_video_comments)

    return sum([s1, s2, s3, s4, s5])


def compute_stats(user):
    """
    计算用户的三项状态值（均为 0-10 浮点数）：

    - attack  (红·攻击力)：给他人视频的有效评论数 / 全班最高数 × 10
    - defense (蓝·防御力)：自己所发视频的被评分平均分（直接使用 0-10 原始分）
    - magic   (紫·魔力)  ：自己视频被收藏总数 / 全班最高数 × 10
    """
    from sqlalchemy import func as sqlfunc

    # ── Attack （只统计有意义的视频评论，排除自己的视频）──
    user_attack = db.session.query(sqlfunc.count(VideoComment.id))\
        .join(Video, VideoComment.video_id == Video.id)\
        .filter(VideoComment.user_id == user.id,
                Video.user_id != user.id,
                VideoComment.is_meaningful == True).scalar() or 0

    attack_sub = db.session.query(
        VideoComment.user_id,
        sqlfunc.count(VideoComment.id).label('cnt')
    ).join(Video, VideoComment.video_id == Video.id)\
     .filter(Video.user_id != VideoComment.user_id,
             VideoComment.is_meaningful == True)\
     .group_by(VideoComment.user_id).subquery()

    max_attack = db.session.query(sqlfunc.max(attack_sub.c.cnt)).scalar() or 1
    attack = min(round(user_attack / max_attack * 10, 1), 10.0)

    # ── Defense ──
    avg_raw = db.session.query(sqlfunc.avg(VideoRating.value))\
        .join(Video, VideoRating.video_id == Video.id)\
        .filter(Video.user_id == user.id).scalar()
    defense = round(float(avg_raw), 1) if avg_raw else 0.0

    # ── Magic ──
    user_magic = db.session.query(sqlfunc.count(VideoFavorite.id))\
        .join(Video, VideoFavorite.video_id == Video.id)\
        .filter(Video.user_id == user.id).scalar() or 0

    magic_sub = db.session.query(
        Video.user_id,
        sqlfunc.count(VideoFavorite.id).label('cnt')
    ).join(VideoFavorite, VideoFavorite.video_id == Video.id)\
     .group_by(Video.user_id).subquery()

    max_magic = db.session.query(sqlfunc.max(magic_sub.c.cnt)).scalar() or 1
    magic = min(round(user_magic / max_magic * 10, 1), 10.0)

    return {'attack': attack, 'defense': defense, 'magic': magic}


# ── 词云签到（Phase 5）──────────────────────────────────────────────

import secrets as _secrets

class SignSession(db.Model):
    """签到会话：教师创建，学生通过 token 链接/二维码参与"""
    __tablename__ = 'sign_session'
    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(20), unique=True, nullable=False,
                           default=lambda: _secrets.token_urlsafe(10))
    question   = db.Column(db.String(500), nullable=False,
                           default='请用一个词描述今天的学习收获')
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active  = db.Column(db.Boolean, default=True)
    responses  = db.relationship('SignResponse', backref='session',
                                 lazy=True, cascade='all, delete-orphan')

class SignResponse(db.Model):
    """单条学生回答；每个学号每次会话只能提交一次"""
    __tablename__ = 'sign_response'
    id         = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sign_session.id'), nullable=False)
    student_id = db.Column(db.String(20), nullable=False)
    answer     = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # 设备溯源（最简记录，用于异常提交核查，仅保留 IP 和 UA 摘要）
    ip_addr    = db.Column(db.String(45),  nullable=True)   # 客户端 IP（含 IPv6）
    ua_hint    = db.Column(db.String(120), nullable=True)   # User-Agent 前120字符
    device_fp  = db.Column(db.String(64),  nullable=True)   # 前端设备指纹（canvas+屏幕+UA hash）
    __table_args__ = (
        db.UniqueConstraint('session_id', 'student_id', name='uq_sign_session_student'),
    )


class HtmlInteraction(db.Model):
    """学生操作 PPT 中 HTML 动画页的记录（签到关联积分用）。
    每学号每课件每 HTML 页只记一次（唯一约束），不管打开/操作几次。
    签到结束时按学号统计不同 HTML 页数 ×0.2 分；未签到学生无 cookie 不记录。"""
    __tablename__ = 'html_interaction'
    id            = db.Column(db.Integer, primary_key=True)
    student_id    = db.Column(db.String(20), nullable=False)
    ppt_session_id = db.Column(db.Integer, db.ForeignKey('ppt_session.id'), nullable=False)
    slide_path    = db.Column(db.String(200), nullable=False)   # HTML 幻灯片文件名
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('student_id', 'ppt_session_id', 'slide_path', name='uq_html_interact'),
    )


# ── PPT课堂（Phase 5 - 6-2）──────────────────────────────────

class PptSession(db.Model):
    """PPT课件会话——每个 token 对应一次课"""
    __tablename__  = 'ppt_session'
    id            = db.Column(db.Integer, primary_key=True)
    token         = db.Column(db.String(20), unique=True, nullable=False,
                              default=lambda: _secrets.token_urlsafe(10))
    title         = db.Column(db.String(200), default='课堂课件')
    slides_json   = db.Column(db.Text, default='[]')   # [{type,path,name}]
    current_slide = db.Column(db.Integer, default=0)
    group_label   = db.Column(db.String(50), nullable=True, default='默认')  # 课程分组标签
    is_shared     = db.Column(db.Boolean, default=False)  # 是否对学生开放回看
    created_by    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    interactions  = db.relationship('PptInteraction', backref='session',
                                    lazy=True, cascade='all, delete-orphan')

class PptInteraction(db.Model):
    """课堂互动——选择 / 投票 / 简答"""
    __tablename__ = 'ppt_interaction'
    id           = db.Column(db.Integer, primary_key=True)
    session_id   = db.Column(db.Integer, db.ForeignKey('ppt_session.id'), nullable=False)
    itype        = db.Column(db.String(20), nullable=False)   # choice | vote | short
    question     = db.Column(db.String(200))                  # 题目文本（语音输入后存储）
    options_json = db.Column(db.Text)                         # JSON: choice→[A,B,C] vote→[蓝方,红方]
    is_active    = db.Column(db.Boolean, default=True)
    is_blocked   = db.Column(db.Boolean, default=False)       # 屏蔽后不参与结束积分召回
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    responses    = db.relationship('PptResponse', backref='interaction',
                                   lazy=True, cascade='all, delete-orphan')

class PptResponse(db.Model):
    """学生互动回答；每学号每次互动只能提交一次"""
    __tablename__  = 'ppt_response'
    id             = db.Column(db.Integer, primary_key=True)
    interaction_id = db.Column(db.Integer, db.ForeignKey('ppt_interaction.id'), nullable=False)
    student_id     = db.Column(db.String(20), nullable=False)
    answer         = db.Column(db.String(20))       # A/B/C | 1/2 | 'short'
    reason         = db.Column(db.String(300), nullable=True)   # 投票理由 / 简答正文
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    # 设备溯源（最简记录，用于异常提交核查）
    ip_addr        = db.Column(db.String(45),  nullable=True)
    ua_hint        = db.Column(db.String(120), nullable=True)
    __table_args__ = (
        db.UniqueConstraint('interaction_id', 'student_id', name='uq_ppt_resp_stu'),
    )


def send_notification(user_id, title, content, ntype='system',
                       link=None, sender_id=None):
    """
    工具函数：向指定用户发送一条通知。
    可在任意路由/服务中调用，调用后需 db.session.commit()。

    示例::
        send_notification(
            user_id=video.user_id,
            title='你的视频收到新评论',
            content=f'{commenter.username} 评论了《{video.title}》',
            ntype='new_comment',
            link=f'/video/{video.id}',
            sender_id=commenter.id
        )
        db.session.commit()
    """
    notif = Notification(
        user_id=user_id,
        sender_id=sender_id,
        type=ntype,
        title=title,
        content=content,
        link=link,
    )
    db.session.add(notif)
    return notif


# ── 站点全局配置（Phase 7：图谱模块 + 论坛隐藏）─────────────────────

class SiteConfig(db.Model):
    """
    全局键值配置表，每行存一个配置项。
    当前使用的 key：
      forum_hidden   - '1' 表示论坛对学生不可见，'0' 或不存在时可见
    """
    __tablename__ = 'site_config'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False, default='')

    @classmethod
    def get(cls, key, default=''):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))


# ── 积分流水账（PointLog）────────────────────────────────────────────

class PointLog(db.Model):
    """
    积分流水账：记录每条积分变动原因。
    reason 枚举（对应前端可读描述）：
      sign_in            签到参与 +1
      interact_join      课堂互动参与 +1
      interact_rank_1    最快第1名 +2
      interact_rank_2    第2名 +1.5
      interact_rank_3    第3名 +1
      interact_rank_4_5  第4-5名 +0.5
      interact_rank_6_8  第6-8名 +0.3
      interact_rank_9_10 第9-10名 +0.2
      medal_gold         视频金牌 +5
      medal_silver       视频银牌 +4
      medal_bronze       视频铜牌 +3
      closest_score      和教师评分最接近 +1
      champion_defend    守擂成功 +2
      champion_win       打擂成功 +2
      battle_win         对战胜利 +1
      battle_lose        对战失败 -1
      battle_streak      连胜奖励
    """
    __tablename__ = 'point_log'

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    points         = db.Column(db.Float,   nullable=False)        # 可为负数
    reason         = db.Column(db.String(40), nullable=False)
    ref_video_id   = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=True)
    ref_session_id = db.Column(db.Integer, nullable=True)         # ppt_session.id 或 sign_session.id
    memo           = db.Column(db.String(100), nullable=True)     # 可读注释
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])


class PointSettlement(db.Model):
    """
    积分结算记录：防止重复结算同一视频的同一类奖励。
    每条记录 (video_id, award_type) 唯一。
    award_type:
      medal         金/银/铜牌奖励
      closest_score 与教师评分最接近
      champion      打擂/守擂
    """
    __tablename__ = 'point_settlement'

    id         = db.Column(db.Integer, primary_key=True)
    video_id   = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    award_type = db.Column(db.String(20), nullable=False)
    settled_at = db.Column(db.DateTime, default=datetime.utcnow)
    settled_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('video_id', 'award_type', name='uq_settlement_video_type'),
    )


# ── 知识图谱慕课视频（Phase 7）────────────────────────────────────────

class GraphVideo(db.Model):
    """
    知识图谱 level2 节点对应的慕课视频。
    每个 KG 节点（node_id 对应 KG.json 中的 id 字段）最多绑定一条记录。
    上传/替换逻辑与作业视频相同，仅教师可操作。
    """
    __tablename__ = 'graph_video'
    id         = db.Column(db.Integer, primary_key=True)
    node_id    = db.Column(db.String(100), unique=True, nullable=False)  # KG.json 节点 id
    url        = db.Column(db.String(200), nullable=False)
    thumbnail  = db.Column(db.String(200), nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    uploader = db.relationship('User', foreign_keys=[uploaded_by])


# ── 打擂台系统（Champion）─────────────────────────────────────────────

class Champion(db.Model):
    """
    擂主记录：记录每道题当前的守擂者。
    problem_key 格式："章节号-题号"，如 "3-7"，对应 QG.json 中 level3 节点。
    同一 problem_key 只有一条记录（当新人成为擂主时更新此行）。
    玩法：
      - 视频评分人数 >= 5 且评论数 >= 3 方可守擂
      - 用户同时持有擂主席位最多 5 个
      - 被更高分者超越后自动失去擂主（更新记录），无需手动放弃
    """
    __tablename__ = 'champion'

    id           = db.Column(db.Integer, primary_key=True)
    problem_key  = db.Column(db.String(20), unique=True, nullable=False)  # "3-7"
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    video_id     = db.Column(db.Integer, db.ForeignKey('video.id'), nullable=False)
    declared_at  = db.Column(db.DateTime, default=datetime.utcnow)

    user  = db.relationship('User',  foreign_keys=[user_id])
    video = db.relationship('Video', foreign_keys=[video_id])


# ══════════════════════════════════════════════════════════════════════
# ── 对战卡牌系统（Battle Card System）────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

class SkillCard(db.Model):
    """技能卡：每个视频最多生成一张，终身绑定视频。"""
    __tablename__ = 'skill_cards'
    id         = db.Column(db.Integer, primary_key=True)
    video_id   = db.Column(db.Integer, db.ForeignKey('video.id'), unique=True, nullable=False)
    owner_id   = db.Column(db.Integer, db.ForeignKey('user.id'),  nullable=False)
    card_type  = db.Column(db.String(10), nullable=False)   # 'melee'|'ranged'|'magic'
    star       = db.Column(db.Integer,  nullable=False)      # 1-5
    damage     = db.Column(db.Float,    nullable=False)      # 最终伤害值
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    video      = db.relationship('Video', backref=db.backref('skill_card', uselist=False))
    owner      = db.relationship('User',  backref='skill_cards')


class BattlePool(db.Model):
    """当前加入对战池的学生，存储防守卡顺序。"""
    __tablename__ = 'battle_pool'
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    defense_card_ids = db.Column(db.String(60), nullable=False)  # JSON: "[id1,id2,id3]"
    joined_at        = db.Column(db.DateTime, default=datetime.utcnow)
    user             = db.relationship('User', backref=db.backref('pool_entry', uselist=False))


class BattleRecord(db.Model):
    """对战记录（一场完整对战）。"""
    __tablename__ = 'battle_records'
    id           = db.Column(db.Integer, primary_key=True)
    attacker_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    defender_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # NULL=教师
    winner       = db.Column(db.String(10), nullable=True)  # 'attacker'|'defender'|'draw'
    is_teacher   = db.Column(db.Boolean, default=False)     # 防守方是否为教师
    atk_hp_end   = db.Column(db.Float)
    def_hp_end   = db.Column(db.Float)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    rounds       = db.relationship('BattleRound', backref='battle',
                                   lazy=True, cascade='all, delete-orphan')
    attacker     = db.relationship('User', foreign_keys=[attacker_id],
                                   backref='battles_as_attacker')
    defender     = db.relationship('User', foreign_keys=[defender_id],
                                   backref='battles_as_defender')


class BattleRound(db.Model):
    """对战单回合详情，供前端动画回放。"""
    __tablename__ = 'battle_rounds'
    id               = db.Column(db.Integer, primary_key=True)
    battle_id        = db.Column(db.Integer, db.ForeignKey('battle_records.id'), nullable=False)
    round_no         = db.Column(db.Integer, nullable=False)   # 1/2/3
    atk_card_id      = db.Column(db.Integer, db.ForeignKey('skill_cards.id'), nullable=True)
    def_card_id      = db.Column(db.Integer, db.ForeignKey('skill_cards.id'), nullable=True)
    atk_final_damage = db.Column(db.Float)
    def_final_damage = db.Column(db.Float)
    atk_hp_after     = db.Column(db.Float)
    def_hp_after     = db.Column(db.Float)
    type_relation    = db.Column(db.String(12))  # 'advantage'|'disadvantage'|'neutral'
    atk_shout        = db.Column(db.String(200))
    def_shout        = db.Column(db.String(200))
    atk_card         = db.relationship('SkillCard', foreign_keys=[atk_card_id])
    def_card         = db.relationship('SkillCard', foreign_keys=[def_card_id])


class PlayerHP(db.Model):
    """玩家每日 HP（懒重置：查询时若日期已过则自动归100）。"""
    __tablename__ = 'player_hp'
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    hp         = db.Column(db.Float, default=100.0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', backref=db.backref('hp_record', uselist=False))


class UserBattleProfile(db.Model):
    """对战相关的用户数据（昵称、连胜、每日计数），独立表避免污染 User。"""
    __tablename__ = 'user_battle_profile'
    user_id          = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    nickname         = db.Column(db.String(20), nullable=True)
    consecutive_wins = db.Column(db.Integer, default=0)
    # 每日懒重置字段
    atk_count_today  = db.Column(db.Integer, default=0)    # 今日主动出击次数
    pts_lost_today   = db.Column(db.Float,   default=0.0)  # 今日已扣积分（主动+被动合计）
    last_reset_date  = db.Column(db.Date, nullable=True)
    user             = db.relationship('User', backref=db.backref('battle_profile', uselist=False))


# ── 视频作业配置（Phase 8）────────────────────────────────────────────

class VideoWorkConfig(db.Model):
    """
    视频作业配置：有效作业数量及各作业对应章节映射（全局唯一行，id=1）。
    n:             有效视频作业数量（1-5，默认 3）
    mappings_json: JSON 数组，每元素为 {"video_index": i, "chapters": [int, ...]}
                   chapters 中的值为章节整数编号（对应 Video.chapter 字段）
    """
    __tablename__ = 'video_work_config'

    id            = db.Column(db.Integer, primary_key=True)
    n             = db.Column(db.Integer, default=3)
    mappings_json = db.Column(db.Text,    default='[]')
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              onupdate=datetime.utcnow)
    updated_by    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    @classmethod
    def get_config(cls):
        """获取全局配置（自动初始化默认值）"""
        cfg = cls.query.first()
        if not cfg:
            cfg = cls()
            db.session.add(cfg)
            db.session.commit()
        return cfg

    def get_mappings(self):
        import json as _json
        try:
            return _json.loads(self.mappings_json or '[]')
        except Exception:
            return []

    def set_mappings(self, value):
        import json as _json
        self.mappings_json = _json.dumps(value, ensure_ascii=False)


class UserVideoFinalScore(db.Model):
    """
    每个学生的视频作业最终成绩（随 VideoWorkConfig 更新时重算）。
    score:       各有效视频作业最高分的平均值（0-10 浮点数）
    detail_json: 各视频作业得分明细，格式：{"1": 8.5, "2": 7.0, ...}
    """
    __tablename__ = 'user_video_final_score'

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'),
                            nullable=False, unique=True)
    score       = db.Column(db.Float,   default=0.0)
    detail_json = db.Column(db.Text,    default='{}')
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])


# ── AI 评分后台任务追踪 ──────────────────────────────────────────────

class AIScoringJob(db.Model):
    """追踪手动触发的 AI 评分批处理任务的进度"""
    __tablename__ = 'ai_scoring_job'

    id          = db.Column(db.Integer, primary_key=True)
    status      = db.Column(db.String(20),  default='idle')  # idle | running | done | error
    total       = db.Column(db.Integer, default=0)   # 待处理总数
    processed   = db.Column(db.Integer, default=0)   # 已成功
    failed      = db.Column(db.Integer, default=0)   # 失败数
    current     = db.Column(db.String(200))           # 当前正在处理的视频标题
    error_msg   = db.Column(db.String(500))           # 错误信息
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get_singleton(cls):
        """获取唯一的任务追踪记录（不存在则创建）"""
        job = cls.query.first()
        if job is None:
            job = cls(status='idle', total=0, processed=0, failed=0, current='')
            db.session.add(job)
            db.session.commit()
        return job