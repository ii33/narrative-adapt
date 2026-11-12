"""Every scene traces current local timeline IDs through the shared Operations view."""
import shutil
from copy import deepcopy

import pytest

from narrative.operations import Operations


def build_sources_template(tmp_path_factory):
    """整套来源标注工程只建一次，每个用例复制目录再打开。

    夹具 `sources_template` 放在 `conftest.py`：pytest 按「提出请求的模块」解析
    夹具参数，放在本模块里，那些 `from test_screenplay_source_references import
    prepared` 的模块会报 `fixture 'sources_template' not found`。
    """
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp('screenplay-sources')
    mp.setenv('NARRATIVE_PROVIDER_DB', str(root / 'providers.sqlite3'))
    try:
        ids = _build_sources(root)
    finally:
        mp.undo()
    return {'root': root, 'ids': ids}


def _build_sources(root):
    ops = Operations.create_graph_project(root / 'sources.nproj')
    text = '甲在车站收到通知。甲在车站确认新安排。'
    source = ops.import_source('来源标注', text)
    result = ops.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '甲', 'quotes': ['甲在车站收到通知。']},
        {'type': 'location', 'name': '车站', 'quotes': ['车站']},
        {'type': 'scene', 'name': '车站沟通'},
        {'type': 'event', 'name': '收到通知', 'scene': '@3', 'location': '@2',
         'participants': ['@1'], 'quotes': ['甲在车站收到通知。']},
        {'type': 'event', 'name': '确认安排', 'scene': '@3', 'location': '@2',
         'participants': ['@1'], 'quotes': ['甲在车站确认新安排。']},
    ], 'properties': []})
    assert result['status'] == 'accepted', result
    events = [n['id'] for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'event']
    adaptation = ops.import_adaptation(events, 0, '建立隔离来源')
    script = ops.create_screenplay('来源剧本', reason='隔离测试')['screenplay']
    local = ops.import_screenplay_timeline(script['id'], events, 0, adaptation['revision'], '独立导入')
    span = ops.create_adaptation_span(script['id'], '通知与安排', selection_ids=events,
        expected_timeline_revision=local['revision'], reason='指定范围')['span']
    segment = ops.create_script_segment(script['id'], span['id'], '通知段落', script['structure_revision'], '创建段落')
    return script['id'], segment['id'], events


@pytest.fixture
def prepared(sources_template, tmp_path, monkeypatch):
    """每例一份独立副本：用例都会写库，共享一个可写工程必然串味。

    副本仍叫 `sources.nproj` 且落在 tmp_path 根下，用例里的路径写法照旧成立。
    """
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    shutil.copytree(sources_template['root'], tmp_path, dirs_exist_ok=True)
    script_id, segment_id, events = sources_template['ids']
    return Operations.open(tmp_path / 'sources.nproj'), script_id, segment_id, events


def submit(ops, segment, plans):
    context = ops.read_scene_table_context(segment)
    return ops.submit_scene_table(segment, [{'plan': p} for p in plans], context['expected_outline_revision'],
        context['span_fingerprint'], context['context_fingerprint'], reason='明确每场来源')


def test_single_scene_keeps_sources_optional_without_claiming_whole_range(prepared):
    ops, screenplay, segment, events = prepared
    plans = [{'summary': '甲在车站处理通知。'}]
    before = deepcopy(plans)
    result = submit(ops, segment, plans)
    unit = result['units'][0]
    refs = unit['source_references']
    assert plans == before
    assert unit['plan']['source_event_ids'] == []
    assert refs['derived'] is False and refs['nodes'] == []
    assert unit['source_reference_text'] == ''
    timeline = ops.read_screenplay_timeline(screenplay)
    assert refs['timeline_id'] == timeline['timeline_id'] and refs['timeline_revision'] == timeline['revision']
    assert ops.read_screenplay(screenplay)['segments'][0]['units'][0]['source_references'] == refs
    assert ops.read_script_unit_context(unit['id'])['unit']['source_reference_text'] == unit['source_reference_text']
    assert ops.read_scene_table_context(segment)['segment']['units'][0]['source_references'] == refs
    context = ops.read_script_unit_context(unit['id'])
    assert context['generation_allowed']
    assert context['creation_scope']['effective_event_ids'] == events
    assert not any(i['code'] == 'SCENE_SOURCE_REQUIRED' for i in ops.check_screenplay(screenplay)['issues'])


def test_multiple_missing_scene_sources_do_not_block_submission_or_writing(prepared):
    ops, _script, segment, events = prepared
    result = submit(ops, segment, [{'summary': '收到通知。'}, {'summary': '确认安排。'}])
    assert result['status'] == 'accepted', result
    units = ops.read_script_segment(segment)['units']
    assert [u['plan']['source_event_ids'] for u in units] == [[], []]
    assert all(not u['source_references']['derived'] for u in units)
    assert all(ops.read_script_unit_context(u['id'])['generation_allowed'] for u in units)
    assert all(ops.read_script_unit_context(u['id'])['creation_scope']['effective_event_ids'] == events for u in units)


def test_same_event_can_support_multiple_scenes_without_partition_quota(prepared):
    ops, _script, segment, events = prepared
    result = submit(ops, segment, [{'summary': '通知的正面表达。', 'source_event_ids': [events[0]]},
                                  {'summary': '通知的另一视角。', 'source_event_ids': [events[0]]}])
    assert result['status'] == 'accepted', result
    assert [u['source_references']['nodes'][0]['id'] for u in result['units']] == [events[0], events[0]]


def test_manual_multi_scene_insertion_allows_omitted_sources(prepared):
    ops, _script, segment, _events = prepared
    current = ops.read_script_segment(segment)
    result = ops.apply_scene_table_changes(segment, [
        {'op': 'insert', 'unit': {'plan': {'summary': '第一场。'}}},
        {'op': 'insert', 'unit': {'plan': {'summary': '第二场。'}}},
    ], current['outline_revision'], reason='手动缺失来源')
    assert result['status'] == 'accepted'
    assert len(ops.read_script_segment(segment)['units']) == 2


def test_manual_plan_edit_can_clear_optional_sources(prepared):
    ops, _script, segment, events = prepared
    saved = submit(ops, segment, [{'summary': '收到通知。', 'source_event_ids': [events[0]]},
                                 {'summary': '确认安排。', 'source_event_ids': [events[1]]}])
    unit = saved['units'][0]
    result = ops.apply_scene_table_changes(segment, [{'op': 'update', 'unit_id': unit['id'],
        'changes': {'plan': unit['plan'] | {'source_event_ids': []}}}], saved['outline_revision'], reason='测试来源不能丢失')
    assert result['status'] == 'accepted'
    assert ops.read_script_segment(segment)['units'][0]['plan']['source_event_ids'] == []
    assert ops.read_script_unit_context(unit['id'])['generation_allowed']


def test_suppressed_source_remains_visible_and_body_is_not_erased(prepared):
    ops, screenplay, segment, events = prepared
    result = submit(ops, segment, [{'summary': '收到通知。', 'source_event_ids': [events[0]]}])
    unit = result['units'][0]
    body = ops.apply_script_block_changes(unit['id'], [{'op': 'insert', 'block': {
        'type': 'action', 'payload': {'text': '甲抬头看通知。'}}}], [], unit['body_revision'], reason='保留正文')
    timeline = ops.read_screenplay_timeline(screenplay)
    ops.suppress_screenplay_timeline_node(screenplay, events[0], expected_timeline_revision=timeline['revision'], reason='测试来源失效')
    view = ops.read_script_segment(segment)['units'][0]
    assert view['source_references']['nodes'][0]['status'] == 'suppressed'
    assert events[0] in view['source_reference_text'] and '已屏蔽' in view['source_reference_text']
    assert view['blocks'] == body['blocks']
    assert not ops.read_script_unit_context(unit['id'])['generation_allowed']
    assert any(i['code'] == 'SCENE_SOURCE_UNAVAILABLE' for i in ops.check_screenplay(screenplay)['issues'])
