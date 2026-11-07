"""Chapter-sized batches extend verified narrative identities, not chapter copies."""
from copy import deepcopy

from narrative.operations import Operations


def test_cross_chapter_batches_reuse_scene_and_event_preserving_evidence_and_members(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'cross-chapter.nproj')
    beginning = '会议室里，双方开始核对交付条件。'
    continuation = '核对仍在继续，双方确认交付日期与验收方式。'
    conclusion = '双方随后签署协议，完成合作确认。'
    text = f'第一章\n{beginning}\n第二章\n{continuation}\n{conclusion}'
    source = ops.import_source('跨章连续会议', text)
    boundary = text.index('第二章')
    first = ops.submit_extraction(source, {'nodes': [
        {'type': 'location', 'name': '会议室', 'quotes': ['会议室']},
        {'type': 'scene', 'name': '会议室交付条件核对与签约'},
        {'type': 'event', 'name': '双方核对交付条件', 'scene': '@2',
         'location': '@1', 'quotes': [beginning]},
    ], 'properties': []}, start=0, end=boundary)
    assert first['status'] == 'accepted', first
    before = ops.graph_snapshot()
    scene = next(row for row in before['nodes'] if row['node_type'] == 'scene')
    event = next(row for row in before['nodes'] if row['node_type'] == 'event')
    location = next(row for row in before['nodes'] if row['node_type'] == 'location')
    old_spans = deepcopy(event['evidence_spans'])
    assert scene['events'] == [event['id']]

    second = ops.submit_extraction(source, {'nodes': [
        {'type': 'scene', 'name': scene['name'], 'existing_id': scene['id'],
         'children': ['@2', '@3']},
        {'type': 'event', 'name': event['name'], 'existing_id': event['id'],
         'scene': '@1', 'quotes': [continuation]},
        {'type': 'event', 'name': '双方签署交付协议', 'scene': '@1',
         'location': location['id'], 'quotes': [conclusion]},
    ], 'properties': []}, start=boundary, end=len(text))
    assert second['status'] == 'accepted', second
    assert second['submission_status'] == 'committed'
    assert second['revision'] == before['revision'] + 1
    after = ops.graph_snapshot()
    scenes = [row for row in after['nodes'] if row['node_type'] == 'scene']
    events = [row for row in after['nodes'] if row['node_type'] == 'event']
    assert len(scenes) == 1 and scenes[0]['id'] == scene['id']
    assert len(events) == 2
    continued = next(row for row in events if row['id'] == event['id'])
    added = next(row for row in events if row['id'] != event['id'])
    assert scenes[0]['events'] == [event['id'], added['id']]
    assert all(span in continued['evidence_spans'] for span in old_spans)
    assert {span['quote'] for span in continued['evidence_spans']} == {beginning, continuation}
    assert {span['quote'] for span in scenes[0]['evidence_spans']} == {beginning, continuation, conclusion}
    for row in (*scenes, *events):
        assert all(span['source_id'] == source and text[span['start']:span['end']] == span['quote']
                   for span in row['evidence_spans'])
    assert ops.source(source)['text'] == text
    assert ops.list_extraction_submissions()['submissions'] == []
    assert ops.check_project()['counts']['error'] == 0
