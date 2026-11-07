"""Usage is observed optional provenance, never inferred from candidate scope."""
from test_screenplay_source_references import prepared as prepared
from test_screenplay_source_references import submit

from narrative.screenplay_scene_policy import scene_event_usage, screenplay_event_usage


def unit(identifier, sources, *, body=True, enabled=True):
    return {'id': identifier, 'kind': 'scene', 'enabled': enabled,
            'plan': {'source_event_ids': sources}, 'blocks': [{'type': 'action'}] if body else []}


def resolved(events=('E1', 'E2')):
    return {'status': 'valid', 'span': {'timeline_id': 'local-timeline'},
            'input': {'source_event_ids': list(events)}}


def test_missing_optional_sources_are_unknown_not_unused_or_all_used():
    usage = scene_event_usage([unit('scene-one', [])], resolved())
    assert usage['used_event_ids'] == usage['unused_event_ids'] == []
    assert usage['unknown_event_ids'] == ['E1', 'E2']
    assert usage['unbound_unit_ids'] == ['scene-one']
    assert not usage['known']


def test_mechanically_inherited_scope_is_not_explicit_usage():
    scene = unit('scene-one', ['E1', 'E2']) | {'source_reference_origin': 'single_scene'}
    usage = scene_event_usage([scene], resolved())
    assert usage['used_event_ids'] == usage['unused_event_ids'] == []
    assert usage['unknown_event_ids'] == ['E1', 'E2'] and not usage['known']


def test_only_existing_body_and_explicit_attribution_count_as_used():
    usage = scene_event_usage([unit('scene-one', ['E1']), unit('scene-two', ['E2'], body=False)], resolved())
    assert usage['used_event_ids'] == ['E1']
    assert usage['unknown_event_ids'] == ['E2']
    assert usage['unused_event_ids'] == [] and not usage['known']


def test_many_scenes_may_use_one_event_leaving_another_observably_unused():
    usage = scene_event_usage([unit('scene-one', ['E1']), unit('scene-two', ['E1'])], resolved())
    assert usage['used_event_ids'] == ['E1'] and usage['unused_event_ids'] == ['E2']
    assert usage['unknown_event_ids'] == [] and usage['known']


def test_disabled_scene_does_not_claim_usage():
    usage = scene_event_usage([unit('scene-one', ['E1']), unit('disabled-scene', ['E2'], enabled=False)], resolved())
    assert usage['used_event_ids'] == ['E1'] and usage['unused_event_ids'] == ['E2']


def test_invalid_provenance_does_not_make_remaining_events_look_unused():
    usage = scene_event_usage([unit('scene-one', ['E1', 'missing'])], resolved())
    assert usage['used_event_ids'] == ['E1']
    assert usage['unknown_event_ids'] == ['E2'] and usage['unused_event_ids'] == []
    assert not usage['known']


def test_screenplay_unions_segments_and_preserves_unknown_scope():
    timeline = {'timeline_id': 'local-timeline', 'nodes': [
        {'id': 'E1', 'node_type': 'event'}, {'id': 'E2', 'node_type': 'event'},
        {'id': 'E3', 'node_type': 'event'}, {'id': 'hidden', 'node_type': 'event', 'suppressed': True}]}
    segments = [{'event_usage': scene_event_usage([unit('first', ['E1'])], resolved(('E1', 'E2')))},
                {'event_usage': scene_event_usage([unit('second', [])], resolved(('E3',)))}]
    usage = screenplay_event_usage(timeline, segments)
    assert usage['timeline_id'] == 'local-timeline'
    assert usage['used_event_ids'] == ['E1'] and usage['unused_event_ids'] == ['E2']
    assert usage['unknown_event_ids'] == ['E3'] and not usage['known']


def test_operations_views_refresh_usage_without_creating_content(prepared):
    ops, screenplay, segment, events = prepared
    saved = submit(ops, segment, [{'summary': '甲抬头读通知。', 'source_event_ids': [events[0]]}])
    scene = saved['units'][0]
    before = ops.graph_snapshot()
    pending_usage = ops.read_screenplay(screenplay)['event_usage']
    assert pending_usage['used_event_ids'] == [] and not pending_usage['known']
    ops.apply_script_block_changes(scene['id'], [{'op': 'insert', 'block': {
        'type': 'action', 'payload': {'text': '甲抬头看通知。'}}}], [], scene['body_revision'], reason='隔离已有正文')
    usage = ops.read_screenplay(screenplay)['event_usage']
    assert usage['used_event_ids'] == [events[0]]
    assert usage['unused_event_ids'] == [events[1]] and usage['known']
    assert ops.read_script_segment(segment)['event_usage']['used_event_ids'] == [events[0]]
    assert ops.graph_snapshot() == before


def test_unbound_scene_body_can_be_saved_without_warning_or_fake_output_note(prepared):
    ops, screenplay, segment, events = prepared
    saved = submit(ops, segment, [{'summary': '甲处理通知。'}])
    scene = saved['units'][0]
    ops.apply_script_block_changes(scene['id'], [{'op': 'insert', 'block': {
        'type': 'action', 'payload': {'text': '甲处理通知。'}}}], [], scene['body_revision'], reason='无强制来源的正文')
    view = ops.read_screenplay(screenplay)
    assert view['segments'][0]['units'][0]['source_reference_text'] == ''
    assert view['event_usage']['unknown_event_ids'] == events
    assert view['event_usage']['unused_event_ids'] == []
    assert ops.read_script_unit_context(scene['id'])['generation_allowed']
    assert not any(i['code'] == 'SCENE_SOURCE_REQUIRED' for i in ops.check_screenplay(screenplay)['issues'])
    assert '尚未标注' not in ops.export_screenplay(screenplay, 'text')['text']
    assert not any(e['kind'] == 'source_reference' for e in ops.compile_screenplay(screenplay, 'text')['elements'])
