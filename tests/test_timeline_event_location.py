"""Event-owned location is mandatory in independent adaptation/script timelines."""
import copy

import pytest
from agent_fixtures import project as project

from narrative.adaptation_operations import _issues


def seed_local(ops):
    scene = ops.edit_adaptation_node(changes={'node_type': 'scene', 'name': '门口'}, reason='明确场景')
    scene_id = scene['created_node_id']
    place = ops.edit_adaptation_node(changes={'node_type': 'location', 'name': '大门'}, reason='明确地点')
    place_id = place['created_node_id']
    event = ops.edit_adaptation_node(changes={'node_type': 'event', 'name': '甲等候', 'initial_location': place_id},
                                     parent_id=scene_id, reason='事件及明确地点')
    return scene_id, place_id, event['created_node_id']


def location_issues(state):
    return [issue for issue in _issues(state) if issue['code'].startswith('EVENT_LOCATION')]


def test_local_event_create_stages_missing_location_and_explicit_locations_pass(project):
    source = copy.deepcopy(project.graph_snapshot())
    scene = project.edit_adaptation_node(changes={'node_type': 'scene', 'name': '场景'}, reason='测试')
    before = copy.deepcopy(project.adaptation_snapshot())
    missing = project.edit_adaptation_node(changes={'node_type': 'event', 'name': '未交代地点'}, parent_id=scene['created_node_id'], reason='不可猜测地点')
    assert missing['status'] == 'staged'
    assert any(p['path'] == ['changes', 'initial_location'] for p in missing['problems'])
    assert project.adaptation_snapshot() == before
    repaired = project.patch_content_submission(missing['submission_id'], missing['submission_revision'],
        [{'op': 'set', 'path': ['changes', 'initial_location'], 'value': '未知'}], '明确补充未知地点')
    assert repaired['status'] == 'accepted'
    for value in ['无', '未知']:
        event = project.edit_adaptation_node(changes={'node_type': 'event', 'name': '明确' + value, 'initial_location': value}, parent_id=scene['created_node_id'], reason='明确地点状态')
        assert event['status'] == 'accepted'
    assert project.check_adaptation()['ok']
    assert project.graph_snapshot() == source


def test_location_static_ignores_metadata_and_inactive_but_not_cognition_or_clear(project):
    _, place, event = seed_local(project)
    base = project.adaptation_snapshot()
    assert not location_issues(base)
    for invalid in ['missing', 'cognition', 'wrong_anchor', 'clear']:
        state = copy.deepcopy(base)
        row = next(r for r in state['properties'] if r.get('node_id') == event)
        if invalid == 'missing':
            state['properties'].remove(row)
        elif invalid == 'cognition':
            row.update(scope='cognition', holder_id='absent-person')
        elif invalid == 'wrong_anchor':
            state['state_anchors'] = [{'id': 'state-boundary', 'node_type': 'event', 'context_id': 'canon',
                                      'source_position': 10, 'offset': 0.5, 'preceding_ids': [], 'following_ids': []}]
            row['event_id'] = 'state-boundary'
        else:
            row['op'] = 'clear'
            row.pop('value', None)
        assert location_issues(state), invalid
    for flag in ['suppressed', 'retracted']:
        state = copy.deepcopy(base)
        state['properties'] = [r for r in state['properties'] if r.get('node_id') != event]
        next(n for n in state['nodes'] if n['id'] == event)[flag] = True
        assert not location_issues(state)
    state = copy.deepcopy(base)
    state['state_anchors'] = [{'id': 'boundary-event', 'node_type': 'event', 'context_id': 'canon',
                             'source_position': 10, 'offset': 0.5, 'preceding_ids': [], 'following_ids': []}]
    assert not location_issues(state)


def test_script_copy_import_move_and_location_clear_are_atomic_and_isolated(project):
    scene, place, event = seed_local(project)
    source = copy.deepcopy(project.graph_snapshot())
    adaptation = copy.deepcopy(project.adaptation_snapshot())
    script = project.create_screenplay('独立剧本', reason='新建')['screenplay']['id']
    project.import_screenplay_timeline(script, [scene], 0, adaptation['revision'], '显式导入')
    local = project.read_screenplay_timeline(script)
    copied = project.copy_screenplay_timeline_node(script, event, expected_timeline_revision=local['revision'], reason='复制事件及地点')
    assert copied['status'] == 'accepted'
    clone = copied['copied_node_id']
    assert any(r.get('node_id') == clone and r.get('event_id') == clone for r in copied['properties'])
    moved = project.move_screenplay_timeline_node(script, clone, after_id=event, expected_timeline_revision=copied['revision'], reason='移动事件')
    assert moved['status'] == 'accepted'
    property_id = next(r['property_id'] for r in moved['properties'] if r.get('node_id') == event)
    before = project.read_screenplay_timeline(script)
    staged = project.set_screenplay_timeline_property(script, event, property_id, event, 'clear', expected_timeline_revision=before['revision'], reason='不能把缺失当未知')
    assert staged['status'] == 'staged'
    assert staged['problems'][0]['path'] == ['op']
    assert project.read_screenplay_timeline(script) == before
    assert project.adaptation_snapshot() == adaptation
    assert project.graph_snapshot() == source
    renamed = project.edit_screenplay_timeline_node(script, event, {'name': '新名'}, expected_timeline_revision=before['revision'], reason='改名无需重交地点')
    assert renamed['status'] == 'accepted'
    invalid = project.edit_screenplay_timeline_node(script, event, {'initial_location': scene}, expected_timeline_revision=renamed['revision'], reason='拒绝场景冒充地点')
    assert invalid['status'] == 'staged'
    assert invalid['problems'][0]['path'] == ['changes', 'initial_location']

def test_immediate_location_record_delete_rejects_without_staging_or_partial_write(project):
    _, _, event = seed_local(project)
    before = copy.deepcopy(project.adaptation_snapshot())
    record = next(r['id'] for r in before['properties'] if r.get('node_id') == event)
    with pytest.raises(ValueError, match='地点'):
        project.delete_adaptation_property_changes([record], expected_revision=before['revision'], reason='即时删除不能留下无地点事件')
    assert project.adaptation_snapshot() == before
    # A concrete existing location can be explicitly replaced by no/unknown.
    changed = project.edit_adaptation_node(event, {'initial_location': '无'}, expected_revision=before['revision'], reason='明确无地点')
    assert changed['status'] == 'accepted' and project.check_adaptation()['ok']
