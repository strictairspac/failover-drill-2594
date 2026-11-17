import os
import tempfile
from datetime import datetime, timedelta

import pytest

DB_FILE = os.path.join(tempfile.mkdtemp(prefix='drill-test-'), 'test.db')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + DB_FILE)
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models.entities import Drill, DrillApply, Node, Scenario, User  # noqa: E402
from src.services.apply_service import request_apply, recover_stale, get_apply  # noqa: E402
from src.services.election import elect  # noqa: E402
from src.utils.security import hash_password, token  # noqa: E402


def base_nodes():
    return [
        dict(id=1, name='hs-primary', role='leader', term=6, log_position=8100,
             available=True, fenced=False, vote_enabled=True),
        dict(id=2, name='hs-east', role='follower', term=6, log_position=8100,
             available=True, fenced=False, vote_enabled=True),
        dict(id=3, name='hs-west', role='follower', term=6, log_position=8098,
             available=True, fenced=False, vote_enabled=True),
    ]


def scenario_dict(fence=True, fail=True):
    return dict(id=1, name='晚班隔离演练', fail_primary=fail, fence_primary=fence,
                max_data_loss=0, required_term=7)


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as session:
        for name, role in [('hs-primary', 'leader'), ('hs-east', 'follower'), ('hs-west', 'follower')]:
            session.add(Node(name=name, role=role, term=6,
                             log_position=8100 if name != 'hs-west' else 8098,
                             available=True, fenced=False, vote_enabled=True))
        session.add(Scenario(name='晚班隔离演练', fail_primary=True, fence_primary=True,
                             max_data_loss=0, required_term=7))
        session.commit()
    yield
    Base.metadata.drop_all(engine)


def make_approved_drill(session, label='晚班切主'):
    session.flush()
    nodes = [{k: getattr(n, k) for k in
              ('id', 'name', 'role', 'term', 'log_position', 'available', 'fenced', 'vote_enabled')}
             for n in session.scalars(select(Node).order_by(Node.id))]
    scenario = scenario_dict()
    result = elect(nodes, scenario)
    assert result['decision'] == 'approved'
    drill = Drill(scenario_id=1, label=label, status='completed',
                  snapshot={'nodes': [dict(n) for n in nodes], 'scenario': dict(scenario)},
                  result=result)
    session.add(drill)
    session.flush()
    return drill


def current_nodes(session):
    return [{'id': n.id, 'name': n.name, 'role': n.role, 'term': n.term}
            for n in session.scalars(select(Node).order_by(Node.id))]


def test_apply_switches_primary_role_term_and_credential(db):
    with Session() as session:
        drill = make_approved_drill(session)
        payload, status = request_apply(session, drill.id, 'operator@hengshi.local')
        session.commit()
    assert status == 200 and payload['status'] == 'applied'
    with Session() as session:
        nodes = {n.name: n for n in session.scalars(select(Node))}
        assert nodes['hs-east'].role == 'leader' and nodes['hs-east'].term == 7
        assert nodes['hs-primary'].role == 'follower' and nodes['hs-primary'].fenced is True
        apply = session.scalar(select(DrillApply))
        assert apply.credential and not apply.credential.startswith('pending:')
        before = {n['name']: n for n in apply.before_nodes}
        after = {n['name']: n for n in apply.after_nodes}
        assert before['hs-primary']['role'] == 'leader'
        assert after['hs-east']['role'] == 'leader' and after['hs-east']['term'] == 7


def test_retry_is_idempotent_and_does_not_bump_term_again(db):
    with Session() as session:
        drill = make_approved_drill(session)
        first, _ = request_apply(session, drill.id, 'operator@hengshi.local')
        session.commit()
    with Session() as session:
        second, status = request_apply(session, drill.id, 'operator@hengshi.local')
        session.commit()
        apply = get_apply(session, drill.id)
        assert status == 200 and second['status'] == 'applied'
        assert second['credential'] == first['credential']
        assert apply.attempts == 1
        nodes = {n.name: n for n in session.scalars(select(Node))}
        assert nodes['hs-east'].term == 7
        assert len(session.scalars(select(DrillApply)).all()) == 1


def test_candidate_lost_after_drill_is_rejected_with_changes(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        east = session.scalar(select(Node).where(Node.name == 'hs-east'))
        east.available = False
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
        assert status == 409 and payload['status'] == 'rejected'
        assert any('不可达' in p for p in payload['problems'])
        assert any(c['node'] == 'hs-east' and c['field'] == 'available' and c['to'] is False
                   for c in payload['changes'])
        stored = session.get(Drill, drill.id)
        assert stored.result['decision'] == 'approved'  # 原演练结论保留
        nodes = {n.name: n for n in session.scalars(select(Node))}
        assert nodes['hs-primary'].role == 'leader' and not nodes['hs-primary'].fenced


def test_competing_drill_cannot_create_double_primary_or_term_regression(db):
    with Session() as session:
        first = make_approved_drill(session, 'A')
        second = make_approved_drill(session, 'B')
        payload_a, status_a = request_apply(session, first.id, 'operator@hengshi.local')
        session.commit()
        payload_b, status_b = request_apply(session, second.id, 'operator@hengshi.local')
        session.commit()
    assert status_a == 200 and payload_a['status'] == 'applied'
    assert status_b == 409 and payload_b['status'] == 'rejected'
    text = ' '.join(payload_b['problems'])
    assert '当前主节点' in text and '任期已变化' in text
    with Session() as session:
        leaders = [n for n in session.scalars(select(Node)) if n.role == 'leader']
        assert [n.name for n in leaders] == ['hs-east']
        assert leaders[0].term == 7
        assert get_apply(session, second.id).status == 'rejected'


def test_tampered_snapshot_evidence_is_rejected(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        drill.snapshot = {**drill.snapshot, 'nodes': [dict(n) for n in drill.snapshot['nodes']]}
        drill.snapshot['nodes'][1]['log_position'] = 9000  # 运维伪造证据
        drill.snapshot['nodes'][1]['available'] = True
        # 使冻结快照与当前节点不一致，重算结果改变
        west = session.scalar(select(Node).where(Node.name == 'hs-west'))
        west.available = False
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
    assert status == 409 and payload['status'] == 'rejected'
    assert payload['problems']


def test_recovery_reruns_interrupted_apply(db):
    with Session() as session:
        drill = make_approved_drill(session)
        apply = DrillApply(drill_id=drill.id, status='applying', credential='crash-1',
                           evidence_sha256=drill.result['input_sha256'],
                           actor='operator@hengshi.local', attempts=1, owner='dead-executor',
                           lease_until=datetime.utcnow() - timedelta(seconds=5),
                           before_nodes=drill.snapshot['nodes'], changes=[])
        session.add(apply)
        session.commit()
    with Session() as session:
        recovered = recover_stale(session)
        session.commit()
        assert len(recovered) == 1 and recovered[0]['status'] == 'applied'
        apply = get_apply(session, drill.id)
        assert apply.status == 'applied' and apply.attempts == 2
        nodes = {n.name: n for n in session.scalars(select(Node))}
        assert nodes['hs-primary'].fenced and nodes['hs-east'].role == 'leader'


def test_late_old_executor_does_not_overwrite_new_primary(db):
    with Session() as session:
        drill_a = make_approved_drill(session, 'A')
        drill_b = make_approved_drill(session, 'B')
        request_apply(session, drill_a.id, 'operator@hengshi.local')
        session.commit()
    # 旧执行者晚到：它手里是 B 的结论，且自己的落实记录停留在 applying、租约已过期
    with Session() as session:
        apply_b = DrillApply(drill_id=drill_b.id, status='applying', credential='late-b',
                             evidence_sha256=drill_b.result['input_sha256'],
                             actor='operator@hengshi.local', attempts=1, owner='slow-old',
                             lease_until=datetime.utcnow() - timedelta(seconds=30),
                             before_nodes=drill_b.snapshot['nodes'], changes=[])
        session.add(apply_b)
        session.commit()
    with Session() as session:
        recovered = recover_stale(session)
        session.commit()
        assert recovered[0]['status'] == 'rejected'
        apply_b = get_apply(session, drill_b.id)
        assert apply_b.status == 'rejected'
        leaders = [n.name for n in session.scalars(select(Node)) if n.role == 'leader']
        assert leaders == ['hs-east']


def test_new_drills_use_applied_nodes(db):
    with Session() as session:
        first = make_approved_drill(session)
        request_apply(session, first.id, 'operator@hengshi.local')
        session.commit()
    with Session() as session:
        nodes = [{k: getattr(n, k) for k in
                  ('id', 'name', 'role', 'term', 'log_position', 'available', 'fenced', 'vote_enabled')}
                 for n in session.scalars(select(Node).order_by(Node.id))]
        assert [n['name'] for n in nodes if n['role'] == 'leader'] == ['hs-east']
        assert max(n['term'] for n in nodes) == 7


# ---------- HTTP 层：权限、页面所需接口 ----------

@pytest.fixture()
def client(db):
    with Session() as session:
        for role in ('admin', 'operator', 'viewer'):
            session.add(User(email=role + '@hengshi.local', role=role,
                             password_hash=hash_password('Drill2026!')))
        session.commit()
    return TestClient(app)


def auth(role):
    return {'Authorization': 'Bearer ' + token(type('U', (), {'email': role + '@hengshi.local',
                                                               'role': role})())}


def test_viewer_cannot_apply_but_can_read(client):
    with Session() as session:
        drill = make_approved_drill(session)
        session.commit()
        drill_id = drill.id
    assert client.post(f'/drills/{drill_id}/apply', headers=auth('viewer')).status_code == 403
    assert client.get('/applies', headers=auth('viewer')).status_code == 200
    assert client.get(f'/drills/{drill_id}', headers=auth('viewer')).status_code == 200


def test_operator_apply_endpoint_and_listing(client):
    with Session() as session:
        drill = make_approved_drill(session)
        session.commit()
        drill_id = drill.id
    response = client.post(f'/drills/{drill_id}/apply', headers=auth('operator'))
    assert response.status_code == 200 and response.json()['status'] == 'applied'
    rows = client.get('/applies', headers=auth('viewer')).json()
    assert rows[0]['drill_id'] == drill_id
    assert rows[0]['before_nodes'] and rows[0]['after_nodes']
    # 再次点击为幂等返回，不重复提升任期
    again = client.post(f'/drills/{drill_id}/apply', headers=auth('operator'))
    assert again.status_code == 200 and again.json()['credential'] == response.json()['credential']
    nodes = client.get('/nodes', headers=auth('viewer')).json()
    assert {n['name']: n['term'] for n in nodes}['hs-east'] == 7


def test_blocked_drill_cannot_be_applied(client):
    with Session() as session:
        drill = make_approved_drill(session)
        drill.result = {**drill.result, 'decision': 'blocked', 'winner': None}
        session.commit()
        drill_id = drill.id
    response = client.post(f'/drills/{drill_id}/apply', headers=auth('operator'))
    assert response.status_code == 409
    assert response.json()['status'] == 'rejected'


def test_admin_recovery_endpoint(client):
    with Session() as session:
        drill = make_approved_drill(session)
        session.add(DrillApply(drill_id=drill.id, status='applying', credential='crash-2',
                               evidence_sha256=drill.result['input_sha256'],
                               actor='operator@hengshi.local', attempts=1, owner='gone',
                               lease_until=datetime.utcnow() - timedelta(seconds=10),
                               before_nodes=drill.snapshot['nodes'], changes=[]))
        session.commit()
    response = client.post('/applies/recover', headers=auth('admin'))
    assert response.status_code == 200 and response.json()['recovered'][0]['status'] == 'applied'
    assert client.post('/applies/recover', headers=auth('operator')).status_code == 403


def test_power_loss_after_node_write_rolls_back_everything(db, monkeypatch):
    """写入后断电：节点切换事务回滚，只留下领取凭据；恢复后重跑并只升一次任期。"""
    from src.services import apply_service

    def boom(*args, **kwargs):
        raise RuntimeError('模拟写入后断电')

    with Session() as session:
        drill = make_approved_drill(session)
        session.commit()
        drill_id = drill.id
        original = apply_service._finish_applied
        monkeypatch.setattr(apply_service, '_finish_applied', boom)
        with pytest.raises(RuntimeError):
            request_apply(session, drill_id, 'operator@hengshi.local')
        monkeypatch.setattr(apply_service, '_finish_applied', original)
    with Session() as session:
        leaders = [n.name for n in session.scalars(select(Node)) if n.role == 'leader']
        assert leaders == ['hs-primary']
        assert session.scalar(select(Node).where(Node.name == 'hs-primary')).fenced is False
        crash = session.scalar(select(DrillApply))
        assert crash.status == 'applying'  # 凭据已落盘，节点未切换
        crash.lease_until = datetime.utcnow() - timedelta(seconds=1)
        session.commit()
    # 租约过期后由恢复流程重跑原操作；不会出现第二个升任期的结果。
    with Session() as session:
        recovered = recover_stale(session)
        session.commit()
        assert len(recovered) == 1 and recovered[0]['status'] == 'applied'
        nodes = {n.name: n for n in session.scalars(select(Node))}
        assert nodes['hs-east'].term == 7 and nodes['hs-primary'].fenced is True
        assert len(session.scalars(select(DrillApply)).all()) == 1



def test_benign_inspection_change_is_recorded_and_still_applies(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        west = session.scalar(select(Node).where(Node.name == 'hs-west'))
        west.log_position = 8099  # 仍不满足 RPO，不改变最优候选与结论
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
    assert status == 200 and payload['status'] == 'applied'
    assert any(c['node'] == 'hs-west' and c['field'] == 'log_position'
               and c['from'] == 8098 and c['to'] == 8099 for c in payload['changes'])
    assert {n['name']: n['role'] for n in payload['after_nodes']}['hs-east'] == 'leader'


def test_added_or_removed_node_rejects_with_clear_change(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        session.add(Node(name='hs-north', role='follower', term=6, log_position=8100,
                         available=True, fenced=False, vote_enabled=True))
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
    assert status == 409 and payload['status'] == 'rejected'
    assert any('节点清单已变化' in p and 'hs-north' in p for p in payload['problems'])
    assert any(c['node'] == 'hs-north' and c['field'] == 'node' for c in payload['changes'])


def test_new_drill_snapshot_uses_applied_nodes_via_http(client):
    with Session() as session:
        drill = make_approved_drill(session)
        session.commit()
        drill_id = drill.id
    assert client.post(f'/drills/{drill_id}/apply', headers=auth('operator')).status_code == 200
    response = client.post('/drills', headers=auth('operator'),
                           json={'scenario_id': 1, 'label': '落实后的下一次演练'})
    assert response.status_code == 202
    snapshot = response.json()['snapshot']['nodes']
    assert [n['name'] for n in snapshot if n['role'] == 'leader'] == ['hs-east']
    assert max(n['term'] for n in snapshot) == 7
    assert next(n for n in snapshot if n['name'] == 'hs-primary')['fenced'] is True


def test_conditional_update_blocks_stale_verdict_race(db, monkeypatch):
    """并发窗口：校验看到的还是旧主，但写节点前状态已被别人改过。

    条件更新必须让整笔落实失败回滚，不能写出第二个主，也不能把任期改小。
    """
    from src.services import apply_service, apply_guard

    real_validate = apply_guard.validate_apply

    def stale_validate(snapshot, result, status, current_nodes):
        # 模拟 TOCTOU：校验仍基于旧快照判定通过，但实际节点已被另一演练切换。
        verdict = real_validate(snapshot, result, status, snapshot['nodes'])
        verdict['current_nodes'] = current_nodes
        return verdict

    monkeypatch.setattr(apply_service, 'validate_apply', stale_validate)

    with Session() as session:
        drill = make_approved_drill(session)
        real_execute = apply_service._execute

        def tampered_execute(db_session, apply_id, owner, actor):
            # 另一个“演练”在独立事务里先完成切换并提交。
            with Session() as other_session:
                other = other_session.scalar(select(Node).where(Node.name == 'hs-east'))
                other.role, other.term = 'leader', 7
                primary = other_session.scalar(select(Node).where(Node.name == 'hs-primary'))
                primary.role, primary.fenced = 'follower', True
                other_session.commit()
            return real_execute(db_session, apply_id, owner, actor)

        monkeypatch.setattr(apply_service, '_execute', tampered_execute)
        with pytest.raises(RuntimeError):
            request_apply(session, drill.id, 'operator@hengshi.local')
    with Session() as session:
        leaders = [n.name for n in session.scalars(select(Node)) if n.role == 'leader']
        assert leaders == ['hs-east']
        assert session.scalar(select(Node).where(Node.name == 'hs-east')).term == 7


def test_rejected_record_keeps_observed_nodes_and_original_drill_result(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        east = session.scalar(select(Node).where(Node.name == 'hs-east'))
        east.fenced = True  # 巡检把候选隔离了
        session.commit()
        original_result = dict(drill.result)
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
    assert status == 409 and payload['status'] == 'rejected'
    observed = {n['name']: n for n in payload['observed_nodes']}
    assert observed['hs-east']['fenced'] is True
    stored = {n['name']: n for n in payload['before_nodes']}
    assert stored['hs-east']['fenced'] is False  # 冻结快照保持不变
    with Session() as session:
        assert session.get(Drill, drill_id).result == original_result
        row = get_apply(session, drill_id)
        assert row.observed_nodes and row.after_nodes is None


def test_rejected_apply_can_be_retried_after_inspection_fix(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        east = session.scalar(select(Node).where(Node.name == 'hs-east'))
        east.available = False
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
        assert status == 409 and payload['status'] == 'rejected'
        assert len(session.scalars(select(DrillApply)).all()) == 1
    # 巡检修复可达性后，同一演练可重新发起；仍是一条记录、凭据随落定更新。
    with Session() as session:
        east = session.scalar(select(Node).where(Node.name == 'hs-east'))
        east.available = True
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
        assert status == 200 and payload['status'] == 'applied'
        rows = session.scalars(select(DrillApply)).all()
        assert len(rows) == 1 and rows[0].attempts == 2
        nodes = {n.name: n for n in session.scalars(select(Node))}
        assert nodes['hs-east'].term == 7 and nodes['hs-primary'].fenced is True


def test_renamed_nodes_reject_instead_of_matching_by_name(db):
    with Session() as session:
        drill = make_approved_drill(session)
        drill_id = drill.id
        # 巡检把原主改名：ID 仍是原主，但冻结证据中的名字已不对应
        primary = session.scalar(select(Node).where(Node.name == 'hs-primary'))
        primary.name = 'hs-primary-renamed'
        session.commit()
    with Session() as session:
        payload, status = request_apply(session, drill_id, 'operator@hengshi.local')
        session.commit()
    assert status == 409 and payload['status'] == 'rejected'
    assert any('改名' in p for p in payload['problems'])
    leaders = [n.name for n in Session().scalars(select(Node)) if n.role == 'leader']
    assert leaders == ['hs-primary-renamed']  # 节点本身没被切换


def test_superseded_executor_returns_current_state_without_writing(db):
    """旧执行者晚到：租约已被接管并落定，它再写也只能看到新主，不得覆盖。"""
    from src.services.apply_service import _execute

    with Session() as session:
        drill = make_approved_drill(session)
        session.commit()
        drill_id = drill.id
    with Session() as session:
        obj = DrillApply(drill_id=drill_id, status='applying', credential='slow-1',
                         evidence_sha256=drill.result['input_sha256'],
                         actor='operator@hengshi.local', attempts=1, owner='slow-executor',
                         lease_until=datetime.utcnow() - timedelta(seconds=5),
                         before_nodes=drill.snapshot['nodes'], changes=[])
        session.add(obj)
        session.commit()
        apply_id = obj.id
    # 恢复者接管并完成
    with Session() as session:
        assert recover_stale(session)[0]['status'] == 'applied'
        session.commit()
    # 旧执行者带着旧 owner 晚到
    with Session() as session:
        result = _execute(session, apply_id, 'slow-executor', 'operator@hengshi.local')
        session.commit()
        assert result.status == 'applied'
        leaders = [n.name for n in session.scalars(select(Node)) if n.role == 'leader']
        assert leaders == ['hs-east']
        node = session.scalar(select(Node).where(Node.name == 'hs-east'))
        assert node.term == 7  # 没有被再升一级
        assert len(session.scalars(select(DrillApply)).all()) == 1
