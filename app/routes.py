from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, jsonify, send_from_directory
from flask_login import login_user, logout_user, login_required, current_user
from app.models import User, Video, VideoRating, VideoFavorite, ForumBoard, Post, Reply, PostImage, ReplyImage, PostLike
from app import db
from app.utils.student_list import validate_student_id, load_student_list
from app.utils.ai_client import async_classify
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename
import os
import time
import subprocess
import uuid
from urllib.parse import quote

main = Blueprint('main', __name__)
auth = Blueprint('auth', __name__)

from app.utils.visibility import hide_test_content as _hide_test_content


def _save_forum_attachment(file_storage):
    original_name = os.path.basename((file_storage.filename or '').strip())
    if not original_name:
        original_name = 'attachment'
    safe_original = secure_filename(original_name)
    _, raw_ext = os.path.splitext(original_name)
    safe_root, safe_ext = os.path.splitext(safe_original)
    ext = safe_ext or raw_ext or ''
    if not safe_root:
        safe_root = 'attachment'
    unique_prefix = f"{time.time_ns()}_{uuid.uuid4().hex[:8]}"
    filename = secure_filename(f"{unique_prefix}_{safe_root}{ext}")
    from app.utils.course_paths import upload_folder as _uf
    folder = os.path.join(_uf(), 'forum')
    os.makedirs(folder, exist_ok=True)
    image_path = os.path.join(folder, filename)
    file_storage.save(image_path)
    return url_for('static', filename=f'uploads/forum/{filename}') + f'?display_name={quote(original_name)}'


@main.route('/forum/attachment/<string:kind>/<int:attachment_id>/download')
def download_forum_attachment(kind, attachment_id):
    model = PostImage if kind == 'post' else ReplyImage if kind == 'reply' else None
    if model is None:
        return 'Invalid attachment type', 404

    attachment = model.query.get_or_404(attachment_id)
    filename = os.path.basename((attachment.url or '').split('?', 1)[0])
    from app.utils.course_paths import upload_folder as _uf
    folder = os.path.join(_uf(), 'forum')
    return send_from_directory(
        folder,
        filename,
        as_attachment=True,
        download_name=attachment.display_name,
    )

# 朋辈助力：章节中文数字
CHAPTER_CN = {1:'一', 2:'二', 3:'三', 4:'四', 5:'五', 6:'六',
              7:'七', 8:'八', 9:'九', 10:'十', 11:'十一', 12:'十二', 13:'十三', 14:'十四'}

def _get_homework_chapters():
    """从当前课程配置获取章节列表"""
    from app.courses import get_course
    return get_course().get('homework_chapters', [2, 3, 4, 5, 6, 7, 8, 9, 10, 13])

def _homework_base_query():
    q = Video.query.filter(
        Video.url.like('%/uploads/%'),
        db.or_(Video.type == 'homework', Video.type == None)
    )
    f = _hide_test_content(Video.user_id)
    return q.filter(f) if f is not None else q

@main.route('/')
@main.route('/homework')
def homework():
    # 通知跳转：?video_id=X → 直接定位到该视频所在章节页
    video_id = request.args.get('video_id', type=int)
    if video_id:
        vid = Video.query.get(video_id)
        if vid and vid.chapter and vid.chapter in _get_homework_chapters():
            return redirect(url_for('main.homework_chapter',
                                    ch=vid.chapter,
                                    video_id=video_id))
        # 视频不存在或章节未知时降级到主页（不循环跳转）

    try:
        # 精品视频（评分最高的3个）
        _feat_q = db.session.query(Video)\
            .outerjoin(VideoRating)\
            .filter(
                Video.url.like('%/uploads/%'),
                db.or_(Video.type == 'homework', Video.type == None)
            )
        _ff = _hide_test_content(Video.user_id)
        if _ff is not None:
            _feat_q = _feat_q.filter(_ff)
        featured_videos = _feat_q\
            .group_by(Video.id)\
            .order_by(db.func.avg(VideoRating.value).desc().nullslast())\
            .limit(3)\
            .all()

        # 按章节统计
        chapter_stats = []
        for ch in _get_homework_chapters():
            vids = _homework_base_query().filter(Video.chapter == ch).all()
            champion = None
            best_r = 0.0
            for v in vids:
                r = v.average_rating
                if r > best_r:
                    best_r = r
                    champion = v
            chapter_stats.append({
                'chapter': ch,
                'cn': CHAPTER_CN[ch],
                'video_count': len(vids),
                'champion': champion,
                'best_rating': best_r,
            })

        return render_template('homework.html',
                               featured_videos=featured_videos,
                               chapter_stats=chapter_stats,
                               active_page='homework')
    except Exception as e:
        print("Error in homework route:", str(e))
        return str(e), 500


@main.route('/homework/chapter/<int:ch>')
def homework_chapter(ch):
    if ch not in _get_homework_chapters():
        from flask import abort
        abort(404)
    videos = _homework_base_query()\
        .filter(Video.chapter == ch)\
        .order_by(Video.created_at.desc()).all()
    return render_template('homework_chapter.html',
                           chapter=ch,
                           chapter_cn=CHAPTER_CN[ch],
                           videos=videos,
                           active_page='homework')


def _is_crane(v):
    return (v.subject == 'crane') or ('crane' in (v.title or '').lower()) or ('立卷夹钳' in (v.title or ''))

def _is_dumper(v):
    return (v.subject == 'dumper') or ('dumper' in (v.title or '').lower()) or ('翻车机' in (v.title or ''))

def _defense_base_query():
    q = Video.query.filter(
        db.or_(Video.type == 'flipped', Video.type == 'cooperation', Video.type == 'defense')
    )
    f = _hide_test_content(Video.user_id)
    return q.filter(f) if f is not None else q

@main.route('/defense')
@main.route('/cooperation')
def cooperation():
    """同心协力页面 - 展示答辩次数分类入口"""
    all_videos = _defense_base_query().all()

    crane_all  = [v for v in all_videos if _is_crane(v)]
    dumper_all = [v for v in all_videos if _is_dumper(v)]
    unmatched  = [v for v in all_videos if not _is_crane(v) and not _is_dumper(v)]
    dumper_all = dumper_all + unmatched

    SESSIONS = list(range(1, 7))  # 第1~6次答辩

    def _session_stats(videos_of_subject):
        stats = []
        for s in SESSIONS:
            vids = [v for v in videos_of_subject if v.class_no == s]
            stats.append({'session': s, 'video_count': len(vids)})
        return stats

    return render_template(
        'defense.html',
        active_page='cooperation',
        crane_sessions=_session_stats(crane_all),
        dumper_sessions=_session_stats(dumper_all),
        crane_total=len(crane_all),
        dumper_total=len(dumper_all),
    )


@main.route('/cooperation/session/<subject>/<int:session_no>')
def cooperation_session(subject, session_no):
    if subject not in ('crane', 'dumper') or session_no not in range(1, 7):
        from flask import abort
        abort(404)
    all_videos = _defense_base_query().order_by(Video.created_at.desc()).all()
    if subject == 'crane':
        base = [v for v in all_videos if _is_crane(v)]
        case_name = '双臂立卷夹钳'
        color_cls = 'crane'
    else:
        crane_ids = {v.id for v in all_videos if _is_crane(v)}
        base = [v for v in all_videos if v.id not in crane_ids]
        case_name = '全机械式翻车机'
        color_cls = 'dumper'
    videos = [v for v in base if v.class_no == session_no]
    return render_template('defense_session.html',
                           subject=subject,
                           case_name=case_name,
                           color_cls=color_cls,
                           session_no=session_no,
                           videos=videos,
                           active_page='cooperation')

@main.route('/forum')
def forum():
    # 论坛对学生隐藏时，非教师重定向回首页
    from app.models import SiteConfig
    if SiteConfig.get('forum_hidden', '0') == '1':
        if not current_user.is_authenticated or not current_user.is_teacher:
            flash('论坛暂时关闭')
            return redirect(url_for('main.homework'))
    # 获取所有板块
    boards = ForumBoard.query.all()
    return render_template('forum.html',
                         boards=boards,
                         active_page='forum')

@main.route('/forum/board/<int:board_id>')
def forum_board(board_id):
    board = ForumBoard.query.get_or_404(board_id)
    page = request.args.get('page', 1, type=int)
    q = Post.query.filter_by(board_id=board_id)
    f = _hide_test_content(Post.author_id)
    if f is not None:
        q = q.filter(f)
    posts = q.order_by(Post.created_at.desc()).paginate(page=page, per_page=200)

    # 板块1：传递当前用户的点赞列表
    liked_post_ids = set()
    if current_user.is_authenticated and board_id == 1:
        liked = PostLike.query.filter(
            PostLike.user_id == current_user.id,
            PostLike.post_id.in_([p.id for p in posts.items])
        ).all()
        liked_post_ids = {l.post_id for l in liked}

    return render_template('forum/board.html',
                         board=board,
                         posts=posts,
                         liked_post_ids=liked_post_ids,
                         active_page='forum')

@main.route('/forum/post/<int:post_id>/html/download')
def download_post_html(post_id):
    """下载帖子嵌入的 HTML 代码"""
    post = Post.query.get_or_404(post_id)
    if not post.html_code:
        return '该帖子没有 HTML 代码', 404
    from flask import Response
    return Response(
        post.html_code,
        mimetype='text/html',
        headers={'Content-Disposition': f'attachment; filename="post_{post_id}.html"'}
    )


@main.route('/forum/post/<int:post_id>')
def post_detail(post_id):
    # 获取帖子详情
    post = Post.query.get_or_404(post_id)
    # 过滤测试账号的回复
    rq = Reply.query.filter_by(post_id=post_id).order_by(Reply.created_at.asc())
    rf = _hide_test_content(Reply.author_id)
    if rf is not None:
        rq = rq.filter(rf)
    replies = rq.all()
    replies_count = len(replies)
    liked = current_user.is_authenticated and \
        PostLike.query.filter_by(user_id=current_user.id, post_id=post_id).first() is not None
    return render_template('forum/post_detail.html',
                         post=post,
                         replies=replies,
                         replies_count=replies_count,
                         liked=liked,
                         active_page='forum')

@main.route('/forum/post/create', methods=['GET', 'POST'])
@login_required
def create_post():
    if request.method == 'POST':
        board_id = request.form.get('board_id')
        title = request.form.get('title')
        content = request.form.get('content')
        
        post = Post(
            title=title,
            content=content,
            board_id=board_id,
            author_id=current_user.id
        )
        db.session.add(post)

        # 处理图片上传
        if 'images' in request.files:
            images = request.files.getlist('images')
            for image in images:
                if image and allowed_image(image.filename):
                    post_image = PostImage(
                        url=_save_forum_attachment(image),
                        post=post
                    )
                    db.session.add(post_image)

        # 处理 HTML 代码（板块1专用）
        html_code = request.form.get('html_code', '').strip()
        if html_code and int(board_id) == 1:
            post.html_code = html_code

        db.session.commit()
        return redirect(url_for('main.post_detail', post_id=post.id))
    
    boards = ForumBoard.query.all()
    selected_board_id = request.args.get('board_id', type=int)
    return render_template('forum/create_post.html',
                         boards=boards,
                         selected_board_id=selected_board_id,
                         active_page='forum')

@main.route('/forum/post/<int:post_id>/reply', methods=['POST'])
@login_required
def add_reply(post_id):
    post = Post.query.get_or_404(post_id)
    content = request.form.get('content')
    
    reply = Reply(
        content=content,
        post_id=post_id,
        author_id=current_user.id
    )
    db.session.add(reply)
    
    # 处理回复中的图片
    if 'images' in request.files:
        images = request.files.getlist('images')
        for image in images:
            if image and allowed_image(image.filename):
                reply_image = ReplyImage(
                    url=_save_forum_attachment(image),
                    reply=reply
                )
                db.session.add(reply_image)
    
    db.session.commit()
    # 后台异步判断论坛回复是否有意义
    async_classify(current_app._get_current_object(),
                   'Reply', reply.id, content)
    return redirect(url_for('main.post_detail', post_id=post_id))

def allowed_image(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

@main.route('/profile')
@login_required
def profile():
    """个人主页：展示自己的视频、收藏、帖子和通知"""
    videos = Video.query.filter_by(user_id=current_user.id)\
        .order_by(Video.created_at.desc()).all()

    from app.models import VideoFavorite, VideoComment, Notification, compute_stars, compute_stats, StarConfig
    favorited_videos = Video.query\
        .join(VideoFavorite)\
        .filter(VideoFavorite.user_id == current_user.id)\
        .order_by(Video.created_at.desc()).all()

    forum_posts = Post.query.filter_by(author_id=current_user.id)\
        .order_by(Post.created_at.desc()).all()

    video_comments_count = VideoComment.query.filter_by(user_id=current_user.id).count()

    notifications = Notification.query\
        .filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc())\
        .limit(50).all()

    unread_count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False).count()

    user_stars = compute_stars(current_user)
    user_stats = compute_stats(current_user)
    star_config = StarConfig.get_config()

    # 全局积分排名（只统计学生）
    from app.models import User as _User
    reward_rank = None
    if not current_user.is_teacher:
        higher_count = _User.query.filter(
            _User.is_teacher == False,
            _User.reward_points > (current_user.reward_points or 0)
        ).count()
        reward_rank = higher_count + 1

    # 对战功能开关
    from app.models import SiteConfig, SkillCard
    battle_open = SiteConfig.get('battle_open', '0') == '1'
    # 当前用户已生成卡牌的视频 id 集合
    carded_video_ids = set(
        r.video_id for r in SkillCard.query.filter_by(owner_id=current_user.id).all()
    ) if not current_user.is_teacher else set()

    return render_template('profile.html',
                           title='个人空间',
                           user=current_user,
                           videos=videos,
                           favorited_videos=favorited_videos,
                           forum_posts=forum_posts,
                           video_comments_count=video_comments_count,
                           notifications=notifications,
                           unread_count=unread_count,
                           user_stars=user_stars,
                           user_stats=user_stats,
                           star_config=star_config,
                           reward_rank=reward_rank,
                           battle_open=battle_open,
                           carded_video_ids=carded_video_ids)

@auth.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        student_id = request.form.get('student_id')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            flash('两次输入的密码不一致')
            return redirect(url_for('auth.register'))

        # 学号名单校验（名单文件存在时生效）
        valid, msg = validate_student_id(student_id)
        if not valid:
            flash(msg)
            return redirect(url_for('auth.register'))

        if User.query.filter_by(student_id=student_id).first():
            flash('该学号已被注册')
            return redirect(url_for('auth.register'))
        
        user = User(username=username, student_id=student_id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash('注册成功，请登录')
        return redirect(url_for('auth.login'))
    
    return render_template('register.html')

@auth.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        student_id = request.form.get('student_id')
        password = request.form.get('password')
        user = User.query.filter_by(student_id=student_id).first()
        
        if user and user.check_password(password):
            login_user(user)
            from flask import session, g
            session['course_key'] = g.course_key  # 记录登录时的课程，跨课访问时强制登出
            # 密码仍为初始学号时，提示修改
            if not user.is_teacher and user.check_password(user.student_id):
                flash('安全提示：您的密码仍为初始密码（学号），请尽快唤改密码以保护账号安全。')
                return redirect(url_for('main.profile') + '#change-password')
            return redirect(url_for('main.homework'))
        
        flash('学号或密码错误')
    
    return render_template('login.html')

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.homework'))

@main.route('/upload-video', methods=['GET', 'POST'])
@login_required
def upload_video():
    if request.method == 'POST':
        if 'video' not in request.files:
            flash('没有选择视频文件')
            return redirect(request.url)

        video_file = request.files['video']
        if video_file.filename == '':
            flash('没有选择视频文件')
            return redirect(request.url)

        if video_file and allowed_file(video_file.filename):
            video_type = request.form.get('video_type', 'homework')  # 'homework' | 'flipped'

            # 自动生成视频标题
            if video_type == 'homework':
                subject = request.form.get('subject', '')
                chapter = request.form.get('chapter', '')
                problem_no = request.form.get('problem_no', '')
                # 无科目选择时用课程配置的 title_subject（工程力学/航海力学）
                if not subject:
                    from app.courses import get_course
                    subject = get_course().get('title_subject') or ''
                title = f"{subject}{chapter}章{problem_no}题"
            else:
                group = request.form.get('group_no', '')
                cls = request.form.get('class_no', '')
                title = f"第{group}小组第{cls}次翻转课堂"

            # 生成安全文件名
            filename = secure_filename(f"{current_user.id}_{int(time.time())}_{video_file.filename}")
            from app.utils.course_paths import upload_folder as _uf
            video_path = os.path.join(_uf(), filename)
            video_file.save(video_path)

            # 用 ffmpeg 截取第 0.5 秒封面作为缩略图
            from app.utils.video import generate_thumbnail
            thumbnail_url = generate_thumbnail(video_path, filename, _uf())

            # 创建视频记录，保存所有分类字段
            video = Video(
                title=title,
                url=url_for('static', filename=f'uploads/{filename}'),
                thumbnail=thumbnail_url,
                type=video_type,
                subject=request.form.get('subject'),
                chapter=request.form.get('chapter', type=int),
                problem_no=request.form.get('problem_no', type=int),
                group_no=request.form.get('group_no', type=int),
                class_no=request.form.get('class_no', type=int),
                user_id=current_user.id
            )
            db.session.add(video)
            db.session.commit()

            flash('视频上传成功！')
            return redirect(url_for('main.homework') if video_type == 'homework' else url_for('main.cooperation'))
        else:
            flash('不支持的文件类型')
            return redirect(request.url)

    return render_template('upload_video.html', active_page='homework')

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']

@main.route('/upload_defense_video')
@login_required
def upload_defense_video():
    """显示答辩视频上传页面"""
    return render_template('upload_defense_video.html', active_page='cooperation')

# 搜索路由
@main.route('/search')
def search():
    query = request.args.get('q', '')
    video_type = request.args.get('type', 'homework')
    
    # 构建基础查询
    base_query = Video.query
    
    # 根据类型过滤
    if video_type == 'homework':
        base_query = base_query.filter(Video.type == 'homework')
    elif video_type == 'cooperation':
        base_query = base_query.filter(Video.type == 'flipped')
    
    # 添加搜索条件（标题 + 描述 + 作者用户名）
    if query:
        base_query = base_query.join(User, Video.user_id == User.id).filter(
            or_(
                Video.title.ilike(f'%{query}%'),
                Video.description.ilike(f'%{query}%'),
                User.username.ilike(f'%{query}%')
            )
        )
    
    # 获取结果并按创建时间倒序排序
    videos = base_query.order_by(Video.created_at.desc()).all()
    
    return render_template('search.html',
                         videos=videos,
                         query=query,
                         type=video_type,
                         active_page='search')


# ── 教师専属功能占位路由（第6、7、8阶段实现）────────────────────────

from functools import wraps

def teacher_required(f):
    """装饰器：限制教师账号或高级测试账号（202601-202610）才能访问"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('请先登录')
            return redirect(url_for('auth.login'))
        if not current_user.is_teacher and not current_user.is_adv_test:
            flash('该功能仅教师账号可用')
            return redirect(url_for('main.homework'))
        return f(*args, **kwargs)
    return decorated


@main.route('/teacher/sign')
@login_required
@teacher_required
def teacher_wordcloud():
    """词云签到控制台"""
    return render_template('teacher/sign_control.html', active_page='wordcloud')


@main.route('/teacher/sign/<token>/cloud')
@login_required
@teacher_required
def teacher_sign_cloud(token):
    """实时词云展示大屏"""
    from app.models import SignSession
    # 高级测试账号可查看任意会话；教师只能查看自己创建的
    if current_user.is_adv_test:
        sess = SignSession.query.filter_by(token=token).first_or_404()
    else:
        sess = SignSession.query.filter_by(token=token, created_by=current_user.id).first_or_404()
    return render_template('teacher/sign_cloud.html',
                           active_page='wordcloud', sess=sess)


@main.route('/s/<token>')
def sign_respond_page(token):
    """学生手机答题页（无需登录）"""
    from app.models import SignSession
    sess = SignSession.query.filter_by(token=token).first_or_404()
    return render_template('sign/respond.html', sess=sess)


@main.route('/teacher/ppt')
@login_required
@teacher_required
def teacher_ppt():
    """PPT课件管理页面"""
    return render_template('teacher/ppt.html', active_page='ppt',
                           is_adv_test=current_user.is_adv_test)


@main.route('/teacher/ppt/<token>/present')
@login_required
@teacher_required
def teacher_ppt_present(token):
    """PPT全屏演示页面"""
    from app.models import PptSession
    # 高级测试账号可查看任意会话；教师只能查看自己创建的
    if current_user.is_adv_test:
        sess = PptSession.query.filter_by(token=token).first_or_404()
    else:
        sess = PptSession.query.filter_by(token=token, created_by=current_user.id).first_or_404()
    is_adv_test = current_user.is_adv_test
    return render_template('teacher/ppt_present.html', active_page='ppt',
                           sess=sess, is_adv_test=is_adv_test)


@main.route('/s/ppt/<token>')
def student_ppt_page(token):
    """学生手机互动页（无需登录）"""
    from app.models import PptSession
    sess = PptSession.query.filter_by(token=token).first_or_404()
    return render_template('sign/ppt_student.html', sess=sess)


@main.route('/student/ppt')
@login_required
def student_ppt_browse():
    """学生回看已分享课堂PPT列表页"""
    return render_template('student/ppt_browse.html', active_page='student_ppt')


@main.route('/student/ppt/<token>/view')
@login_required
def student_ppt_view(token):
    """学生只读PPT播放页"""
    from app.models import PptSession
    sess = PptSession.query.filter_by(token=token, is_shared=True).first_or_404()
    return render_template('student/ppt_view.html', sess=sess, active_page='student_ppt')


@main.route('/teacher/star_config', methods=['GET', 'POST'])
@login_required
@teacher_required
def teacher_star_config():
    """原星级配置路由 → 重定向到新的课程管理页，避免旧链接404"""
    return redirect(url_for('main.teacher_course_mgmt') + '#star')


@main.route('/teacher/course_mgmt')
@login_required
@teacher_required
def teacher_course_mgmt():
    """课程管理页（含星级配置、数据管理、数据分析三个子标签）"""
    from app.models import StarConfig
    cfg = StarConfig.get_config()
    return render_template('teacher/course_mgmt.html',
                           active_page='course_mgmt', cfg=cfg)
@main.route('/battle')
@login_required
def battle():
    """卡牌对战页"""
    return render_template('battle.html', active_page='battle')


@main.route('/homework/<int:video_id>')
def homework_video(video_id):
    """旧路由：重定向到 homework 页并定位视频"""
    from flask import redirect, url_for
    return redirect(url_for('main.homework') + f'?video_id={video_id}')

@main.route('/video/<int:video_id>')
def video_redirect(video_id):
    """通知链接格式 /video/<id> → homework 页"""
    from flask import redirect, url_for
    return redirect(url_for('main.homework') + f'?video_id={video_id}')


# ── Phase 7：知识/问题图谱模块 ─────────────────────────────────────

@main.route('/graph')
@login_required
def graph():
    """力学笃行：知识图谱 + 问题图谱交互页面"""
    from app.models import SiteConfig
    return render_template('graph.html', active_page='graph')


# ── Phase 7：论坛可见性切换（教师专用）────────────────────────────

@main.route('/api/forum/visibility', methods=['POST'])
@login_required
def toggle_forum_visibility():
    """教师一键切换论坛对学生的可见性"""
    if not current_user.is_teacher:
        return jsonify({'message': '无权限'}), 403
    from app.models import SiteConfig
    current = SiteConfig.get('forum_hidden', '0')
    new_val = '0' if current == '1' else '1'
    SiteConfig.set('forum_hidden', new_val)
    db.session.commit()
    return jsonify({'hidden': new_val == '1'})


@main.route('/api/forum_hidden_status')
@login_required
def forum_hidden_status():
    """教师查询论坛当前可见状态"""
    if not current_user.is_teacher:
        return jsonify({'message': '无权限'}), 403
    from app.models import SiteConfig
    return jsonify({'hidden': SiteConfig.get('forum_hidden', '0') == '1'})


@main.route('/KG.json')
@login_required
def kg_json():
    """提供知识图谱数据"""
    import json
    from app.utils.course_paths import kg_path
    path = kg_path()
    with open(os.path.normpath(path), encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@main.route('/QG.json')
@login_required
def qg_json():
    """提供问题图谱数据"""
    import json
    from app.utils.course_paths import qg_path
    path = qg_path()
    with open(os.path.normpath(path), encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)