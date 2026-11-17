"""落实选主结论前的纯规则校验。

本模块只处理普通 dict，不访问数据库、不连接真实集群、不执行生产切主。
把校验与写库分离，便于在“候选失联、竞争落实、旧执行者晚到”等场景下
重复回放同一套安全规则（仍属沙盘推演）。
"""
import hashlib
import json

from src.services.election import elect

NODE_FIELDS = ('role', 'term', 'log_position', 'available', 'fenced', 'vote_enabled')
FIELD_LABELS = {
    'role': '角色', 'term': '任期', 'log_position': '日志位置',
    'available': '可达', 'fenced': '写入隔离', 'vote_enabled': '投票权',
}


def index_by_id(nodes):
    return {n.get('id'): n for n in nodes or []}


def diff_nodes(snapshot_nodes, current_nodes):
    """列出巡检修改造成的字段变化，用于拒绝时说明“变了什么”。"""
    changes = []
    snap = index_by_id(snapshot_nodes)
    cur = index_by_id(current_nodes)
    for node_id in sorted(set(snap) | set(cur), key=lambda value: (value is None, value)):
        if node_id not in snap:
            changes.append({'node': cur[node_id].get('name'), 'field': 'node',
                            'label': '节点清单', 'from': '不存在', 'to': '新增'})
            continue
        if node_id not in cur:
            changes.append({'node': snap[node_id].get('name'), 'field': 'node',
                            'label': '节点清单', 'from': '存在', 'to': '已删除'})
            continue
        for field in NODE_FIELDS:
            before, after = snap[node_id].get(field), cur[node_id].get(field)
            if before != after:
                changes.append({'node': cur[node_id].get('name'), 'field': field,
                                'label': FIELD_LABELS[field], 'from': before, 'to': after})
    return changes


def credential_digest(drill_id, evidence_sha256, before_nodes, after_nodes, new_term, actor):
    payload = {
        'drill_id': drill_id, 'evidence_sha256': evidence_sha256,
        'before_nodes': before_nodes, 'after_nodes': after_nodes,
        'new_term': new_term, 'actor': actor,
    }
    source = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(source.encode()).hexdigest()


def validate_apply(snapshot, result, status, current_nodes):
    """复核“原证据是否仍安全”，并给出原子切换后的节点计划。

    返回结构：
      ok / problems / changes / before_primary / primary_id
      new_primary / new_primary_id / new_term / after_nodes
    """
    snapshot = snapshot or {}
    result = result or {}
    snapshot_nodes = snapshot.get('nodes') or []
    scenario = snapshot.get('scenario') or {}
    current_nodes = list(current_nodes or [])
    verdict = {
        'ok': False, 'problems': [], 'changes': diff_nodes(snapshot_nodes, current_nodes),
        'before_primary': None, 'primary_id': None,
        'new_primary': None, 'new_primary_id': None,
        'new_term': result.get('new_term'), 'current_max_term': None,
        'after_nodes': None,
    }

    def problem(message):
        if message not in verdict['problems']:
            verdict['problems'].append(message)

    if status != 'completed' or not result:
        problem('演练尚未完成，不能落实选主结论')
        return verdict
    if result.get('decision') != 'approved':
        problem('演练结论为安全阻断，不能落实')
        return verdict
    # 凭据完整性：冻结快照必须与演练记录中的输入摘要一致。
    source = json.dumps({'nodes': snapshot_nodes, 'scenario': scenario},
                        sort_keys=True, ensure_ascii=False)
    actual_sha = hashlib.sha256(source.encode()).hexdigest()
    if result.get('input_sha256') != actual_sha:
        problem('冻结快照与演练记录的输入摘要不一致，凭据完整性校验失败')
        return verdict
    winner_name = result.get('winner')
    planned_term = result.get('new_term')
    if not winner_name or not isinstance(planned_term, int):
        problem('演练结论缺少候选节点或新任期，凭据不完整')
        return verdict

    snapshot_leaders = [n for n in snapshot_nodes if n.get('role') == 'leader']
    if len(snapshot_leaders) != 1:
        problem('冻结快照没有唯一的原主节点，凭据不完整')
        return verdict
    snapshot_primary = snapshot_leaders[0]

    snap_ids = set(index_by_id(snapshot_nodes))
    current_ids = set(index_by_id(current_nodes))
    if snap_ids != current_ids:
        added = [n.get('name') for n in current_nodes if n.get('id') not in snap_ids]
        removed = [n.get('name') for n in snapshot_nodes if n.get('id') not in current_ids]
        detail = []
        if added:
            detail.append('新增 ' + '、'.join(str(name) for name in added))
        if removed:
            detail.append('移除 ' + '、'.join(str(name) for name in removed))
        problem('节点清单已变化（' + '；'.join(detail) + '），原多数派证据不再可复现')

    leaders = [n for n in current_nodes if n.get('role') == 'leader']
    if len(leaders) != 1:
        problem(f'当前存在 {len(leaders)} 个主节点；落实前必须恰好一个主节点，拒绝造成双主')
    else:
        leader = leaders[0]
        verdict['before_primary'] = leader.get('name')
        verdict['primary_id'] = leader.get('id')
        if leader.get('id') != snapshot_primary.get('id'):
            problem(f"当前主节点为 {leader.get('name')}，已不是演练冻结的原主 "
                    f"{snapshot_primary.get('name')}；结论可能已被其他演练落实或节点被巡检修改")
        elif leader.get('name') != snapshot_primary.get('name'):
            problem(f"原主节点已由 {snapshot_primary.get('name')} 改名为 {leader.get('name')}，"
                    f"冻结证据中的节点名不再对应")

    current_by_name = {n.get('name'): n for n in current_nodes}
    winner = current_by_name.get(winner_name)
    snapshot_winner = next((n for n in snapshot_nodes if n.get('name') == winner_name), None)
    if winner is None:
        problem(f'候选节点 {winner_name} 已不在当前沙盘节点中')
    elif snapshot_winner and winner.get('id') != snapshot_winner.get('id'):
        problem(f'候选名 {winner_name} 现在对应另一个节点（ID 已从 {snapshot_winner.get("id")} '
                f'变为 {winner.get("id")}），冻结证据不再对应')
    else:
        verdict['new_primary'] = winner.get('name')
        verdict['new_primary_id'] = winner.get('id')
        if winner.get('role') != 'follower':
            problem(f"候选 {winner_name} 当前角色为 {winner.get('role')}，不再是可提升的 follower")
        if not winner.get('available'):
            problem(f'候选 {winner_name} 当前不可达（失联），原证据已不安全')
        if winner.get('fenced'):
            problem(f'候选 {winner_name} 当前处于写入隔离，不能提升为主')
        if not winner.get('vote_enabled'):
            problem(f'候选 {winner_name} 当前没有投票权，不能提升为主')

    if current_nodes:
        current_max_term = max(n.get('term', 0) for n in current_nodes)
        verdict['current_max_term'] = current_max_term
        if planned_term != current_max_term + 1:
            problem(f'任期已变化：当前最大任期 {current_max_term}，原结论新任期 {planned_term}；'
                    f'仅当新任期 = 当前最大任期 + 1 时允许落实，避免任期倒退或同次重试重复升任期')

    # 用当前节点完整重算一次：原结论必须仍可复现，否则视为证据失效。
    try:
        fresh = elect(current_nodes, scenario)
    except (ValueError, KeyError, TypeError) as exc:
        problem(f'按当前节点重算失败：{exc}；原结论不再可复现')
    else:
        if fresh.get('decision') != 'approved':
            failed = '、'.join(c.get('rule', '未知规则') for c in fresh.get('checks', [])
                               if not c.get('pass'))
            problem(f'按当前节点重新复核为阻断（{failed or "规则未通过"}），原证据已不安全')
        else:
            if fresh.get('winner') != winner_name:
                problem(f"按当前节点重新计算的最优候选为 {fresh.get('winner')}，"
                        f"与原结论 {winner_name} 不一致")
            if fresh.get('new_term') != planned_term:
                problem(f"按当前节点重新计算的任期为 {fresh.get('new_term')}，"
                        f"与原结论 {planned_term} 不一致")

    if verdict['problems']:
        return verdict

    after = [dict(n) for n in current_nodes]
    for node in after:
        if node.get('id') == snapshot_primary.get('id'):
            node['role'] = 'follower'
            node['fenced'] = True
        elif node.get('id') == winner.get('id'):
            node['role'] = 'leader'
            node['term'] = planned_term
    after.sort(key=lambda n: n.get('id') or 0)

    leaders_after = [n.get('name') for n in after if n.get('role') == 'leader']
    if leaders_after != [winner_name]:
        problem(f"切换计划未能形成唯一新主（当前计划主节点：{leaders_after}），拒绝执行")
        return verdict
    fenced_after = next((n for n in after if n.get('id') == snapshot_primary.get('id')), None)
    if not fenced_after or not fenced_after.get('fenced'):
        problem('切换计划未隔离原主，拒绝执行')
        return verdict

    verdict['ok'] = True
    verdict['after_nodes'] = after
    return verdict
