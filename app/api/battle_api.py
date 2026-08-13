"""
app/api/battle_api.py
卡牌对战系统 API
===========================================

POST /api/battle/generate_card/<video_id>   生成技能卡
GET  /api/battle/my_cards                   我的所有卡
POST /api/battle/join_pool                  设置防守卡并加入对战池 {card_ids:[id1,id2,id3]}
POST /api/battle/leave_pool                 离开对战池
GET  /api/battle/pool_status                我的HP/场次/是否在池
POST /api/battle/attack                     主动攻击 {card_ids:[id1,id2,id3]}
GET  /api/battle/<int:battle_id>            获取对战回放数据
GET  /api/battle/history                    近期对战记录
POST /api/admin/battle/toggle               教师开关对战 {open:true/false}
"""

import json
import random
from datetime import date, datetime

from flask import request, jsonify
from flask_login import login_required, current_user

from . import api
from app import db
from app.models import (
    SkillCard, BattlePool, BattleRecord, BattleRound,
    PlayerHP, UserBattleProfile, SiteConfig,
    User, Video, VideoComment, VideoFavorite, VideoRating,
    compute_stars, compute_stats,
)

# ═══════════════════════════════════════════════════════════════════════
# 常量 / 词库
# ═══════════════════════════════════════════════════════════════════════
MAX_DAILY_ATTACKS  = 5       # 每日主动攻击上限
MAX_DAILY_PTS_LOSE = 5.0     # 每日最多被扣积分
TEACHER_TRIGGER_P  = 0.10    # 触发教师对战概率
ROUNDS             = 3       # 每场回合数
TEACHER_STUDENT_ID = '0'     # 教师使用的占位 student_id

TYPE_ORDER = {'melee': 'ranged', 'ranged': 'magic', 'magic': 'melee'}  # 克制：key 克 value
# melee > magic > ranged > melee（即 melee 克 magic, magic 克 ranged, ranged 克 melee）
# 实际：melee 被 ranged 克；ranged 被 magic 克；magic 被 melee 克
# 用"谁克我"更直观：BEATEN_BY
BEATEN_BY = {'melee': 'ranged', 'ranged': 'magic', 'magic': 'melee'}

ATTACK_WORDS  = ['冲击','魄力','玄妙','压力','难点','深度','精妙','凶险','奥义','威力','底蕴','灵魂']
DEFEND_WORDS  = ['巧妙','灵活','创意','高效','精妙','优雅','深度','奥义','独到','妙思','严谨','神来之笔']
CARD_TYPES    = ['melee', 'ranged', 'magic']
TYPE_CN       = {'melee': '近战', 'ranged': '远程', 'magic': '魔法'}

TEACHER_SHOUTS = [
    "让我看看你们{title}的计算和讨论都到什么水平了！",
    "哎呀，怎么你们又犯这个错误了，赶紧加紧复习吧。",
    "放下作业...~~~~啊啊啊~~~",
]
TEACHER_NARRATOR = [
    "谜之旁白：由于专注看题，教师造成了{damage}点伤害",
    "谜之旁白：由于担心学生，教师造成了{damage}点伤害",
    "谜之旁白：由于编程久坐肩颈酸痛，教师造成了0点伤害，学生获胜了~~~",
]


# ═══════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════

def _slow_growth_score(n: int, cap: int = 15) -> float:
    """收藏/评论数 → 0-15 分，缓增曲线：sqrt(n/cap)*cap。"""
    if n <= 0:
        return 0.0
    return round(min((n / cap) ** 0.5 * cap, float(cap)), 2)


def _generate_card_star(student_star: int) -> int:
    """根据用户星级按概率生成卡牌星级（1-5）。"""
    probabilities = {
        0: [0.60, 0.30, 0.08, 0.02, 0.00],
        1: [0.50, 0.32, 0.12, 0.05, 0.01],
        2: [0.35, 0.35, 0.18, 0.09, 0.03],
        3: [0.20, 0.30, 0.28, 0.16, 0.06],
        4: [0.10, 0.20, 0.30, 0.28, 0.12],
        5: [0.05, 0.12, 0.25, 0.35, 0.23],
    }
    weights = probabilities.get(student_star, probabilities[0])
    stars = random.choices([1, 2, 3, 4, 5], weights=weights, k=1)[0]
    return stars


def _calc_card_damage(video: Video, owner: User) -> dict:
    """生成卡牌的所有属性 dict（不写库）。"""
    student_star = compute_stars(owner)
    card_star    = _generate_card_star(student_star)
    card_type    = random.choice(CARD_TYPES)

    stats = compute_stats(owner)
    # 基础分：对应属性统计值（0-10）
    if card_type == 'melee':
        base = stats['attack']
    elif card_type == 'magic':
        base = stats['magic']
    else:
        base = (stats['attack'] + stats['defense']) / 2

    # 附加分：来自视频本身（收藏/评论缓增）
    fav_count     = VideoFavorite.query.filter_by(video_id=video.id).count()
    comment_count = VideoComment.query.filter_by(video_id=video.id).count()
    bonus = _slow_growth_score(fav_count) * 0.5 + _slow_growth_score(comment_count) * 0.5

    # 星级倍率：1星=0.6 … 5星=1.4
    star_mult = 0.6 + 0.2 * (card_star - 1)

    damage = round((base + bonus) * star_mult, 2)
    damage = max(1.0, damage)   # 至少1点

    word = random.choice(ATTACK_WORDS)
    card_name = f"{TYPE_CN[card_type]}·{word}·{'★'*card_star}"

    return {
        'card_type': card_type,
        'star':      card_star,
        'damage':    damage,
        'name':      card_name,
    }


def _get_or_create_hp(user_id: int) -> PlayerHP:
    """获取或创建玩家 HP，懒重置（每天 08:00 重置）。"""
    hp_rec = PlayerHP.query.get(user_id)
    if hp_rec is None:
        hp_rec = PlayerHP(user_id=user_id, hp=100.0, updated_at=datetime.utcnow())
        db.session.add(hp_rec)
    else:
        now = datetime.utcnow()
        reset_today = datetime(now.year, now.month, now.day, 0, 0, 0)  # UTC 0 = 北京 08:00
        if hp_rec.updated_at < reset_today:
            hp_rec.hp = 100.0
            hp_rec.updated_at = now
    return hp_rec


def _get_or_create_profile(user_id: int) -> UserBattleProfile:
    """获取或创建对战档案，懒重置每日计数。"""
    profile = UserBattleProfile.query.get(user_id)
    if profile is None:
        profile = UserBattleProfile(user_id=user_id)
        db.session.add(profile)
    else:
        today = date.today()
        if profile.last_reset_date != today:
            profile.atk_count_today = 0
            profile.pts_lost_today  = 0.0
            profile.last_reset_date = today
    return profile


def _type_relation(atk_type: str, def_type: str) -> str:
    """返回攻击方的克制关系：advantage/disadvantage/neutral。"""
    if BEATEN_BY.get(def_type) == atk_type:
        return 'advantage'    # 攻击者克防守者
    if BEATEN_BY.get(atk_type) == def_type:
        return 'disadvantage'
    return 'neutral'


def _damage_multiplier(relation: str, is_attacker: bool) -> float:
    """根据克制关系返回伤害倍率。"""
    if relation == 'advantage':
        return 1.3 if is_attacker else 0.7
    if relation == 'disadvantage':
        return 0.7 if is_attacker else 1.3
    return 1.0


def _card_brief(card) -> dict:
    """卡牌简要信息 dict，供前端渲染。"""
    if card is None:
        return None
    return {
        'id':        card.id,
        'card_type': card.card_type,
        'star':      card.star,
        'damage':    card.damage,
        'video_id':  card.video_id,
        'title':     card.video.title if card.video else '',
    }


def _award_points(user: User, delta: float, reason: str, ref_video_id=None):
    """更新用户积分（直接写 reward_points，无需 PointLog 导入）。"""
    user.reward_points = round((user.reward_points or 0) + delta, 2)


def _run_battle(atk_cards: list, def_cards: list, is_teacher: bool,
                attacker: User, defender_user=None) -> dict:
    """
    执行三回合对战逻辑，返回 rounds 列表和最终 HP。
    atk_cards / def_cards: SkillCard 对象列表（长度 3）
    """
    atk_hp = 100.0
    def_hp = 100.0 if not is_teacher else 100.0

    # 教师防守卡：用平均攻击力 * random(2,3) 伪造 damage
    if is_teacher:
        avg_dmg = sum(c.damage for c in atk_cards) / len(atk_cards) if atk_cards else 5.0
        class _FakeCard:
            def __init__(self, dmg, ctype):
                self.id = None; self.damage = dmg; self.card_type = ctype
                self.video = None; self.star = 3
        def_cards = [_FakeCard(avg_dmg * random.uniform(2, 3), random.choice(CARD_TYPES))
                     for _ in range(ROUNDS)]

    rounds_data = []
    for i in range(ROUNDS):
        ac = atk_cards[i] if i < len(atk_cards) else atk_cards[-1]
        dc = def_cards[i] if i < len(def_cards) else def_cards[-1]

        relation   = _type_relation(ac.card_type, dc.card_type)
        atk_mult   = _damage_multiplier(relation, is_attacker=True)
        def_mult   = _damage_multiplier(relation, is_attacker=False)

        atk_dmg = round(ac.damage * atk_mult, 2)
        def_dmg = round(dc.damage * def_mult, 2)

        # 教师特殊规则：前两回合伤害上限=学生HP*40%；第3回合教师HP必然低于学生剩余HP
        if is_teacher:
            if i == 2:
                # 第3回合：先算学生受到的伤害，得到学生最终HP
                atk_hp_after_r3 = round(max(0.0, atk_hp - def_dmg), 2)
                # 教师受到的伤害 = 教师当前HP - (学生最终HP - 随机5~10)，保证教师HP < 学生HP
                margin   = random.uniform(5, 10)
                target_def_hp = atk_hp_after_r3 - margin
                atk_dmg  = round(max(def_hp - max(target_def_hp, 0.0), 0.0), 2)
                atk_hp   = atk_hp_after_r3
                def_hp   = round(max(0.0, def_hp - atk_dmg), 2)
            else:
                def_dmg  = min(def_dmg, atk_hp * 0.4)
                atk_hp   = round(max(0.0, atk_hp - def_dmg), 2)
                def_hp   = round(max(0.0, def_hp - atk_dmg), 2)
        else:
            atk_hp = round(max(0.0, atk_hp - def_dmg), 2)
            def_hp = round(max(0.0, def_hp - atk_dmg), 2)

        # 生成战斗台词
        atk_title = ac.video.title if ac.video else ''
        if is_teacher:
            def_shout = TEACHER_SHOUTS[i].format(title=atk_title) if i < 2 else TEACHER_SHOUTS[2]
            narrator  = TEACHER_NARRATOR[i].format(damage=def_dmg) if i < 2 else TEACHER_NARRATOR[2]
            def_shout = def_shout + '\n' + narrator
        else:
            def_shout = random.choice(DEFEND_WORDS) + '！'

        atk_word  = random.choice(ATTACK_WORDS)
        atk_shout = f'{atk_word}！造成{atk_dmg}点伤害！'

        rounds_data.append({
            'round_no':         i + 1,
            'atk_card':         _card_brief(ac),
            'def_card':         _card_brief(dc) if not is_teacher else {
                                    'id': None, 'card_type': dc.card_type,
                                    'star': dc.star, 'damage': round(dc.damage, 2),
                                    'video_id': None, 'title': '教师秘密武器'},
            'atk_final_damage': atk_dmg,
            'def_final_damage': def_dmg,
            'atk_hp_after':     atk_hp,
            'def_hp_after':     def_hp,
            'type_relation':    relation,
            'atk_shout':        atk_shout,
            'def_shout':        def_shout,
        })

    if atk_hp > def_hp:
        winner = 'attacker'
    elif def_hp > atk_hp:
        winner = 'defender'
    else:
        winner = 'draw'

    return {'rounds': rounds_data, 'atk_hp_end': atk_hp, 'def_hp_end': def_hp, 'winner': winner}


# ═══════════════════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════════════════

@api.route('/battle/generate_card/<int:video_id>', methods=['POST'])
@login_required
def battle_generate_card(video_id):
    """为指定视频生成技能卡（每视频只能生成一次）。"""
    if current_user.is_teacher:
        return jsonify({'error': '教师不生成卡牌'}), 403

    video = Video.query.get(video_id)
    if not video:
        return jsonify({'error': '视频不存在'}), 404
    if video.user_id != current_user.id:
        return jsonify({'error': '只能为自己的视频生成卡牌'}), 403

    # 已经生成过
    if SkillCard.query.filter_by(video_id=video_id).first():
        return jsonify({'error': '该视频已生成技能卡'}), 400

    attrs = _calc_card_damage(video, current_user)
    card  = SkillCard(
        video_id  = video_id,
        owner_id  = current_user.id,
        card_type = attrs['card_type'],
        star      = attrs['star'],
        damage    = attrs['damage'],
    )
    db.session.add(card)
    db.session.commit()
    return jsonify({'ok': True, 'card': {**_card_brief(card), 'name': attrs['name']}})


@api.route('/battle/my_cards', methods=['GET'])
@login_required
def battle_my_cards():
    """返回当前用户所有技能卡。"""
    cards = SkillCard.query.filter_by(owner_id=current_user.id).all()
    return jsonify({'cards': [_card_brief(c) for c in cards]})


@api.route('/battle/join_pool', methods=['POST'])
@login_required
def battle_join_pool():
    """加入对战池，设置3张防守卡。"""
    if current_user.is_teacher:
        return jsonify({'error': '教师不加入对战池'}), 403

    if SiteConfig.get('battle_open', '0') != '1':
        return jsonify({'error': '对战功能未开放'}), 403

    data     = request.get_json(silent=True) or {}
    card_ids = data.get('card_ids', [])
    if len(card_ids) != 3:
        return jsonify({'error': '请选择恰好3张防守卡'}), 400

    # 验证都是自己的卡
    cards = SkillCard.query.filter(
        SkillCard.id.in_(card_ids), SkillCard.owner_id == current_user.id
    ).all()
    if len(cards) != 3:
        return jsonify({'error': '卡牌不合法'}), 400

    entry = BattlePool.query.filter_by(user_id=current_user.id).first()
    if entry:
        entry.defense_card_ids = json.dumps(card_ids)
        entry.joined_at = datetime.utcnow()
    else:
        entry = BattlePool(user_id=current_user.id, defense_card_ids=json.dumps(card_ids))
        db.session.add(entry)
    db.session.commit()
    return jsonify({'ok': True, 'message': '已加入对战池'})


@api.route('/battle/leave_pool', methods=['POST'])
@login_required
def battle_leave_pool():
    """离开对战池。"""
    entry = BattlePool.query.filter_by(user_id=current_user.id).first()
    if entry:
        db.session.delete(entry)
        db.session.commit()
    return jsonify({'ok': True})


@api.route('/battle/pool_status', methods=['GET'])
@login_required
def battle_pool_status():
    """返回当前用户的 HP、今日场次、是否在池，以及卡池中玩家总数。"""
    hp_rec  = _get_or_create_hp(current_user.id)
    profile = _get_or_create_profile(current_user.id)
    db.session.flush()

    in_pool    = BattlePool.query.filter_by(user_id=current_user.id).first() is not None
    pool_count = BattlePool.query.count()
    my_cards   = SkillCard.query.filter_by(owner_id=current_user.id).count()

    from app.models import compute_stats
    stats = compute_stats(current_user) if not current_user.is_teacher else {'attack': 0, 'defense': 0, 'magic': 0}

    return jsonify({
        'hp':               hp_rec.hp,
        'atk_count_today':  profile.atk_count_today,
        'max_daily_attacks':MAX_DAILY_ATTACKS,
        'consecutive_wins': profile.consecutive_wins,
        'in_pool':          in_pool,
        'pool_count':       pool_count,
        'my_card_count':    my_cards,
        'battle_open':      SiteConfig.get('battle_open', '0') == '1',
        'attack':           stats['attack'],
        'defense':          stats['defense'],
        'magic':            stats['magic'],
        'nickname':         profile.nickname or current_user.username,
    })


@api.route('/battle/attack', methods=['POST'])
@login_required
def battle_attack():
    """
    主动发起攻击。
    Body: { "card_ids": [id1, id2, id3] }  (攻击顺序)
    """
    if current_user.is_teacher:
        return jsonify({'error': '教师不参与攻击'}), 403
    if SiteConfig.get('battle_open', '0') != '1':
        return jsonify({'error': '对战功能未开放'}), 403

    data     = request.get_json(silent=True) or {}
    card_ids = data.get('card_ids', [])
    if len(card_ids) != 3:
        return jsonify({'error': '请选择恰好3张攻击卡'}), 400

    # 验证攻击卡
    atk_cards_raw = SkillCard.query.filter(
        SkillCard.id.in_(card_ids), SkillCard.owner_id == current_user.id
    ).all()
    if len(atk_cards_raw) != 3:
        return jsonify({'error': '卡牌不合法'}), 400
    # 按 card_ids 顺序排列
    card_map  = {c.id: c for c in atk_cards_raw}
    atk_cards = [card_map[cid] for cid in card_ids if cid in card_map]

    my_hp  = _get_or_create_hp(current_user.id)
    my_pro = _get_or_create_profile(current_user.id)
    db.session.flush()

    if my_hp.hp <= 0:
        return jsonify({'error': 'HP 已耗尽，今日无法出战'}), 400
    if my_pro.atk_count_today >= MAX_DAILY_ATTACKS:
        return jsonify({'error': f'今日攻击次数已达上限（{MAX_DAILY_ATTACKS}）'}), 400

    # ── 选择对手 ──
    is_teacher_battle = random.random() < TEACHER_TRIGGER_P

    if is_teacher_battle:
        # 教师对战
        defender_user = None
        def_cards     = []   # _run_battle 内部伪造
    else:
        # 从对战池中选人（排除自己，排除今日已打过的）
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        fought_today = db.session.query(BattleRecord.defender_id).filter(
            BattleRecord.attacker_id == current_user.id,
            BattleRecord.created_at  >= today_start,
        ).subquery()

        # 打过的概率减半：先查全部候选
        all_pool = BattlePool.query.filter(BattlePool.user_id != current_user.id).all()
        if not all_pool:
            # 对战池为空 → 强制教师对战
            is_teacher_battle = True
            defender_user = None
            def_cards = []
        else:
            fought_ids = {r[0] for r in db.session.query(BattleRecord.defender_id).filter(
                BattleRecord.attacker_id == current_user.id,
                BattleRecord.created_at  >= today_start,
            ).all()}

            # 今日已打过同一对手禁止重复
            eligible = [e for e in all_pool if e.user_id not in fought_ids]
            if not eligible:
                return jsonify({'error': '今日已与所有在线玩家各打一场，请明日再来'}), 400

            # 曾打过的权重 0.5，未打过 1.0
            weights = []
            for e in eligible:
                past = BattleRecord.query.filter_by(
                    attacker_id=current_user.id, defender_id=e.user_id
                ).count()
                weights.append(0.5 if past > 0 else 1.0)

            chosen_entry  = random.choices(eligible, weights=weights, k=1)[0]
            defender_user = User.query.get(chosen_entry.user_id)
            def_card_ids  = json.loads(chosen_entry.defense_card_ids)
            def_cards_raw = SkillCard.query.filter(SkillCard.id.in_(def_card_ids)).all()
            dcm           = {c.id: c for c in def_cards_raw}
            def_cards     = [dcm[cid] for cid in def_card_ids if cid in dcm]

    # ── 执行对战 ──
    result = _run_battle(atk_cards, def_cards, is_teacher_battle,
                         current_user, defender_user)
    winner   = result['winner']
    atk_hp_e = result['atk_hp_end']
    def_hp_e = result['def_hp_end']

    # ── 更新 HP ──
    if winner == 'attacker':
        my_hp.hp = min(100.0, my_hp.hp + 50.0)
    elif winner == 'defender':
        my_hp.hp = max(0.0, my_hp.hp - 20.0)  # 被对方防守卡削弱
        # 教师对战不扣减对方HP
        if not is_teacher_battle and defender_user:
            def_hp_rec = _get_or_create_hp(defender_user.id)
            def_hp_rec.hp = max(0.0, def_hp_rec.hp - 20.0)
    else:
        my_hp.hp = min(100.0, my_hp.hp + 15.0)  # 平局小回复

    my_hp.updated_at = datetime.utcnow()

    # ── 更新连胜 & 积分 ──
    # 新规则：赢 +1；2连胜 +1.5；3+连胜每场 +2；输 -0.5，连胜清零
    if winner == 'attacker':
        my_pro.consecutive_wins += 1
        streak = my_pro.consecutive_wins
        if streak == 1:
            pts_gain = 1.0
        elif streak == 2:
            pts_gain = 1.5
        else:
            pts_gain = 2.0
        _award_points(current_user, pts_gain, 'battle_win')
    else:
        my_pro.consecutive_wins = 0
        # 败 -0.5，每日合计上限 5
        remaining_loss = MAX_DAILY_PTS_LOSE - my_pro.pts_lost_today
        actual_loss    = min(0.5, remaining_loss)
        if actual_loss > 0:
            my_pro.pts_lost_today = round(my_pro.pts_lost_today + actual_loss, 2)
            _award_points(current_user, -actual_loss, 'battle_lose')
        pts_gain = 0.0

    my_pro.atk_count_today += 1
    my_pro.last_reset_date  = date.today()

    # ── 被动损失：防守方被打败 ──
    if not is_teacher_battle and defender_user and winner == 'attacker':
        def_pro = _get_or_create_profile(defender_user.id)
        def_remaining = MAX_DAILY_PTS_LOSE - def_pro.pts_lost_today
        def_loss = min(0.5, def_remaining)
        if def_loss > 0:
            def_pro.pts_lost_today = round(def_pro.pts_lost_today + def_loss, 2)
            def_pro.last_reset_date = date.today()
            _award_points(defender_user, -def_loss, 'battle_lose')

    # ── 写入 BattleRecord ──
    rec = BattleRecord(
        attacker_id = current_user.id,
        defender_id = defender_user.id if defender_user else None,
        winner      = winner,
        is_teacher  = is_teacher_battle,
        atk_hp_end  = atk_hp_e,
        def_hp_end  = def_hp_e,
    )
    db.session.add(rec)
    db.session.flush()  # 获取 rec.id

    for rd in result['rounds']:
        row = BattleRound(
            battle_id        = rec.id,
            round_no         = rd['round_no'],
            atk_card_id      = rd['atk_card']['id'] if rd['atk_card'] else None,
            def_card_id      = rd['def_card']['id'] if rd['def_card'] else None,
            atk_final_damage = rd['atk_final_damage'],
            def_final_damage = rd['def_final_damage'],
            atk_hp_after     = rd['atk_hp_after'],
            def_hp_after     = rd['def_hp_after'],
            type_relation    = rd['type_relation'],
            atk_shout        = rd['atk_shout'],
            def_shout        = rd['def_shout'],
        )
        db.session.add(row)

    db.session.commit()

    return jsonify({
        'ok':          True,
        'battle_id':   rec.id,
        'winner':      winner,
        'is_teacher':  is_teacher_battle,
        'defender':    defender_user.username if defender_user else '教师',
        'atk_hp_end':  atk_hp_e,
        'def_hp_end':  def_hp_e,
        'pts_gained':  pts_gain if winner == 'attacker' else 0,
        'consecutive': my_pro.consecutive_wins,
        'rounds':      result['rounds'],
    })


@api.route('/battle/<int:battle_id>', methods=['GET'])
@login_required
def battle_detail(battle_id):
    """获取对战回放数据。"""
    rec = BattleRecord.query.get_or_404(battle_id)
    # 只允许参战双方查看
    if rec.attacker_id != current_user.id and rec.defender_id != current_user.id and not current_user.is_teacher:
        return jsonify({'error': '无权查看'}), 403

    rounds = []
    for r in sorted(rec.rounds, key=lambda x: x.round_no):
        rounds.append({
            'round_no':         r.round_no,
            'atk_card':         _card_brief(r.atk_card),
            'def_card':         _card_brief(r.def_card),
            'atk_final_damage': r.atk_final_damage,
            'def_final_damage': r.def_final_damage,
            'atk_hp_after':     r.atk_hp_after,
            'def_hp_after':     r.def_hp_after,
            'type_relation':    r.type_relation,
            'atk_shout':        r.atk_shout,
            'def_shout':        r.def_shout,
        })

    return jsonify({
        'battle_id':   rec.id,
        'attacker':    rec.attacker.username if rec.attacker else '?',
        'defender':    rec.defender.username if rec.defender else '教师',
        'winner':      rec.winner,
        'is_teacher':  rec.is_teacher,
        'atk_hp_end':  rec.atk_hp_end,
        'def_hp_end':  rec.def_hp_end,
        'created_at':  rec.created_at.strftime('%Y-%m-%d %H:%M'),
        'rounds':      rounds,
    })


@api.route('/battle/history', methods=['GET'])
@login_required
def battle_history():
    """最近 20 场对战记录（主动+被动）。"""
    from sqlalchemy import or_
    records = BattleRecord.query.filter(
        or_(BattleRecord.attacker_id == current_user.id,
            BattleRecord.defender_id == current_user.id)
    ).order_by(BattleRecord.created_at.desc()).limit(20).all()

    result = []
    for r in records:
        role = 'attacker' if r.attacker_id == current_user.id else 'defender'
        opp  = (r.defender.username if r.defender else '教师') if role == 'attacker' \
               else r.attacker.username
        my_win = (winner := r.winner) == role or winner == 'draw'
        result.append({
            'battle_id':  r.id,
            'role':       role,
            'opponent':   opp,
            'winner':     winner,
            'is_teacher': r.is_teacher,
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M'),
        })
    return jsonify({'history': result})


@api.route('/battle/set_nickname', methods=['POST'])
@login_required
def battle_set_nickname():
    """设置对战昵称（最多8个字）。"""
    data     = request.get_json(silent=True) or {}
    nickname = (data.get('nickname') or '').strip()
    if not nickname:
        return jsonify({'error': '昵称不能为空'}), 400
    if len(nickname) > 8:
        return jsonify({'error': '昵称最多8个字'}), 400
    profile = _get_or_create_profile(current_user.id)
    profile.nickname = nickname
    db.session.commit()
    return jsonify({'ok': True, 'nickname': nickname})


@api.route('/admin/battle/toggle', methods=['POST'])
@login_required
def admin_battle_toggle():
    """教师开关对战功能。"""
    if not current_user.is_teacher:
        return jsonify({'error': '无权操作'}), 403
    data = request.get_json(silent=True) or {}
    val  = '1' if data.get('open') else '0'
    SiteConfig.set('battle_open', val)
    db.session.commit()
    return jsonify({'ok': True, 'battle_open': val == '1'})
