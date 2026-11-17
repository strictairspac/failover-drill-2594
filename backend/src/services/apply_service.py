"""把演练结论落实回沙盘节点。

安全要点：
- 同一次演练只有一条落实记录（drill_id 唯一），重试复用，不重复升任期。
- 校验与切换都在行锁事务内完成；原主隔离、新主角色/任期与落实凭据同一提交。
- 不同演练并发落实时，节点行锁把比较与写入串行化，第二笔会看到新主并拒绝。
- 中断后由租约（lease）恢复原操作；旧执行者晚到时重新校验，不会覆盖新主。
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.controllers.actions import audit
from src.constants.enums import APPLY_STATUSES
from src.models.entities import Drill, DrillApply, Node
from src.services.apply_guard import credential_digest, validate_apply
from src.utils.serialize import record

class ApplyConflict(RuntimeError):
    """并发落实或写后自检冲突；事务已回滚，节点保持不变。"""


LEASE_SECONDS = 30
ACTIVE = ('pending', 'applying')
assert set(APPLY_STATUSES) == {'pending', 'applying', 'applied', 'rejected'}
NODE_KEYS = ('id', 'name', 'role', 'term', 'log_position', 'available', 'fenced', 'vote_enabled')


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def apply_record(obj):
    out = record(obj)
    out['before_nodes'] = obj.before_nodes
    out['after_nodes'] = obj.after_nodes
    out['observed_nodes'] = obj.observed_nodes
    out['changes'] = obj.changes or []
    out['problems'] = obj.problems or []
    out['simulation'] = True
    return out


def _slim(nodes):
    if nodes is None:
        return None
    return [{k: n.get(k) for k in NODE_KEYS} for n in nodes]


def _claim(obj, owner):
    obj.status = 'applying'
    obj.owner = owner
    obj.lease_until = utcnow() + timedelta(seconds=LEASE_SECONDS)
    obj.attempts = (obj.attempts or 0) + 1


def _finish_rejected(db, obj, verdict, actor):
    obj.status = 'rejected'
    obj.problems = verdict['problems']
    obj.changes = verdict['changes']
    # before_nodes 保留入队时的冻结快照；observed_nodes 记录拒绝当下的节点，
    # 便于复核“当时为什么拒绝”。
    obj.observed_nodes = _slim(verdict.get('current_nodes'))
    obj.after_nodes = None
    obj.owner = None
    obj.lease_until = None
    db.flush()
    audit(db, actor, f'drill:apply:rejected:{obj.drill_id}')
    return obj


def _finish_applied(db, obj, verdict, actor):
    obj.status = 'applied'
    obj.problems = []
    obj.changes = verdict['changes']
    obj.before_nodes = _slim(verdict['current_nodes'])
    obj.after_nodes = _slim(verdict['after_nodes'])
    obj.observed_nodes = _slim(verdict['current_nodes'])
    obj.credential = credential_digest(
        obj.drill_id, obj.evidence_sha256, obj.before_nodes, obj.after_nodes,
        verdict['new_term'], actor)
    obj.applied_at = utcnow()
    obj.owner = None
    obj.lease_until = None
    db.flush()
    audit(db, actor, f'drill:apply:applied:{obj.drill_id}')
    return obj


def _execute(db, apply_id, owner, actor):
    """在事务内完成一次落实尝试；调用方负责 commit/rollback。

    加锁顺序固定为“演练 → 落实记录 → 节点”，与发起/恢复路径完全一致，避免死锁。
    进入时先核对领取人：若租约已被恢复流程接管、或别的执行者已经落定，
    则直接返回当前记录，绝不重复切换、也不覆盖新主。
    """
    drill_id = db.scalar(select(DrillApply.drill_id).where(DrillApply.id == apply_id))
    if drill_id is None:
        return None
    drill = db.scalar(select(Drill).where(Drill.id == drill_id).with_for_update())
    obj = db.scalar(select(DrillApply).where(DrillApply.id == apply_id).with_for_update())
    if obj is None:
        return None
    if obj.status not in ACTIVE or obj.owner != owner:
        return obj
    if drill is None:
        _finish_rejected(db, obj, {'problems': ['演练不存在'], 'changes': []}, actor)
        return obj
    if drill.status != 'completed' or (drill.result or {}).get('decision') != 'approved':
        _finish_rejected(db, obj, {'problems': ['演练尚未完成或结论不是允许切主，不能落实'],
                                   'changes': []}, actor)
        return obj

    # 固定顺序锁定全部节点，和并发落实保持同一加锁顺序，避免死锁。
    current = list(db.scalars(select(Node).order_by(Node.id).with_for_update()).all())
    current_nodes = [record(n) for n in current]
    verdict = validate_apply(drill.snapshot, drill.result, drill.status, current_nodes)
    verdict['current_nodes'] = current_nodes
    if not verdict['ok']:
        return _finish_rejected(db, obj, verdict, actor)

    former_id, winner_id, new_term = verdict['primary_id'], verdict['new_primary_id'], verdict['new_term']
    # 条件更新做第二道保险：即使数据库隔离级别或去向不明，也不会写出第二个主，
    # 更不会把任期改回较小值。UPDATE 未命中即整笔回滚重试。
    fenced = db.execute(
        update(Node)
        .where(Node.id == former_id, Node.role == 'leader', Node.term <= new_term)
        .values(role='follower', fenced=True))
    promoted = db.execute(
        update(Node)
        .where(Node.id == winner_id, Node.role == 'follower', Node.term < new_term)
        .values(role='leader', term=new_term))
    if fenced.rowcount != 1 or promoted.rowcount != 1:
        raise ApplyConflict('落实冲突：节点状态已被并发修改，已回滚且未改变任何节点，请刷新后重试')
    for node in current:
        db.expire(node)

    # 写后自检：同一事务内必须只有一个主，隔离和任期都到位。
    after = list(db.scalars(select(Node).order_by(Node.id).with_for_update()).all())
    leaders = [n.name for n in after if n.role == 'leader']
    former_after = next(n for n in after if n.id == former_id)
    winner_after = next(n for n in after if n.id == winner_id)
    if leaders != [winner_after.name] or not former_after.fenced or winner_after.term != new_term:
        raise ApplyConflict('落实自检失败：节点未形成唯一新主或任期/隔离不正确，已回滚')
    verdict['after_nodes'] = [record(n) for n in after]
    return _finish_applied(db, obj, verdict, actor)


def get_apply(db, drill_id):
    return db.scalar(select(DrillApply).where(DrillApply.drill_id == drill_id))


def request_apply(db, drill_id, actor):
    """发起或重试落实。返回 (payload, http_status)。

    - 已落实/应用中：返回既有记录，不创建第二份凭据、不重复升任期。
    - 待处理：直接接管执行。
    - 已拒绝：允许重新发起，重置为待处理并按当前节点重新复核。
    - 证据已变化：保留原演练结论，记录 rejected 并说明变化。
    - 领取凭据先落盘，再执行切换；中途中断会留下 applying 记录供恢复重跑。
    """
    return _apply_locked(db, drill_id, actor)


def _apply_locked(db, drill_id, actor):
    # 统一加锁顺序：先演练，再落实记录，最后节点。
    drill = db.scalar(select(Drill).where(Drill.id == drill_id).with_for_update())
    if drill is None:
        return None, 404
    if drill.status != 'completed' or (drill.result or {}).get('decision') != 'approved':
        return {'drill_id': drill_id, 'status': 'rejected', 'changes': [],
                'problems': ['演练尚未完成或结论不是允许切主，不能落实'],
                'simulation': True}, 409

    obj = db.scalar(select(DrillApply).where(DrillApply.drill_id == drill_id).with_for_update())
    if obj is not None:
        if obj.status == 'applied':
            # 已落实：同一凭据、同一任期，重试不会再次提升。
            return apply_record(obj), 200
        if obj.status == 'applying' and obj.lease_until and obj.lease_until > utcnow():
            return apply_record(obj), 202
        if obj.status == 'rejected':
            # 拒绝后允许用户重新发起：重置为待处理，按当前节点重新复核。
            # 原演练结论、冻结快照与拒绝历史仍保留在演练记录和审计中。
            obj.status = 'pending'
            obj.problems = []
            obj.after_nodes = None
            obj.observed_nodes = None

    owner = actor + ':' + uuid.uuid4().hex[:12]
    if obj is not None:
        # 先接管旧记录（pending 或租约已过期的 applying），再进入执行。
        obj.actor = actor
        obj.evidence_sha256 = (drill.result or {}).get('input_sha256') or obj.evidence_sha256
        _claim(obj, owner)
        db.flush()
        apply_id = obj.id
    else:
        snapshot = drill.snapshot or {}
        obj = DrillApply(drill_id=drill.id, status='pending',
                         credential='pending:' + uuid.uuid4().hex,
                         evidence_sha256=(drill.result or {}).get('input_sha256') or '',
                         actor=actor, attempts=0, before_nodes=_slim(snapshot.get('nodes')),
                         changes=[])
        _claim(obj, owner)
        try:
            db.add(obj)
            db.flush()
            apply_id = obj.id
        except IntegrityError:
            # 并发请求已经创建了同一次演练的落实记录，复用它但需重新领取。
            # 数据库的语句级回滚已丢弃本次 INSERT，这里只需把失败对象移出会话。
            db.expunge(obj)
            obj = db.scalar(select(DrillApply)
                            .where(DrillApply.drill_id == drill_id).with_for_update())
            if obj is None:
                raise
            if obj.status == 'applied':
                return apply_record(obj), 200
            if obj.status == 'applying' and obj.lease_until and obj.lease_until > utcnow():
                return apply_record(obj), 202
            obj.actor = actor
            obj.evidence_sha256 = (drill.result or {}).get('input_sha256') or obj.evidence_sha256
            _claim(obj, owner)
            apply_id = obj.id
    # 领取与凭据先提交：若随后进程中断，applying 记录会由租约恢复流程重跑原操作。
    db.commit()

    try:
        obj = _execute(db, apply_id, owner, actor)
        db.commit()
    except Exception:
        # 节点切换与凭据在同一事务；失败则整体回滚，不留下半切换的节点。
        db.rollback()
        raise
    if obj is None:
        return None, 404
    return apply_record(obj), (200 if obj.status == 'applied'
                               else 202 if obj.owner and obj.lease_until and obj.lease_until > utcnow()
                               else 409)


def list_applies(db):
    rows = db.scalars(select(DrillApply).order_by(DrillApply.id.desc()).limit(100)).all()
    return [apply_record(obj) for obj in rows]


def recover_stale(db, actor='system:recovery'):
    """中断恢复：接管租约过期的 pending/applying 记录并重跑原操作。

    先读候选编号（不加锁），再逐条按“演练 → 落实记录 → 节点”的顺序加锁，
    与 request_apply 保持一致，避免与在线落实交叉死锁。
    """
    candidates = db.scalars(
        select(DrillApply.id).where(DrillApply.status.in_(ACTIVE)).order_by(DrillApply.id)).all()
    recovered = []
    for apply_id in candidates:
        obj = db.get(DrillApply, apply_id)
        if obj is None or obj.status not in ACTIVE:
            continue
        if obj.lease_until and obj.lease_until > utcnow():
            continue
        # 按“演练 → 落实记录 → 节点”的顺序加锁，与 request_apply 一致。
        drill = db.scalar(select(Drill).where(Drill.id == obj.drill_id).with_for_update())
        obj = db.scalar(select(DrillApply).where(DrillApply.id == apply_id).with_for_update())
        if obj is None or obj.status not in ACTIVE:
            continue
        if obj.lease_until and obj.lease_until > utcnow():
            continue
        if drill is None:
            # 演练已不存在：不能凭空切换，落成拒绝并保留原因。
            _finish_rejected(db, obj, {'problems': ['演练不存在，无法恢复落实'],
                                       'changes': []}, actor)
            db.commit()
            recovered.append(apply_record(obj))
            continue
        owner = actor + ':' + uuid.uuid4().hex[:8]
        _claim(obj, owner)
        db.commit()
        try:
            obj = _execute(db, apply_id, owner, actor)
            db.commit()
        except ApplyConflict:
            # 单条冲突不影响其他待恢复记录；回滚后继续处理下一条。
            db.rollback()
            continue
        except Exception:
            db.rollback()
            raise
        if obj is not None:
            recovered.append(apply_record(obj))
    return recovered


def recover_worker_once():
    """worker 中调用：独立会话恢复中断的落实。"""
    from src.config.database import Session
    with Session() as db:
        try:
            recovered = recover_stale(db)
            db.commit()
            return recovered
        except Exception:
            db.rollback()
            raise
