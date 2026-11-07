"""Submission feedback retains precise original paths and reference causes."""
from narrative.extraction_staging import _problems


def test_explicit_path_wins_over_message_and_retains_reference_cause():
    diagnostics = [{
        'code': 'INVALID_REFERENCE', 'severity': 'error',
        'message': '地点引用无效，关联nodes[2]缺少引文',
        'path': ['nodes', 4, 'location'],
        'related_paths': [['nodes', 2, 'quotes']],
    }]
    problem, = _problems({}, diagnostics)
    assert problem['path'] == ['nodes', 4, 'location']
    assert (problem['section'], problem['index'], problem['field']) == ('nodes', 4, 'location')
    assert problem['related_paths'] == [['nodes', 2, 'quotes']]
    assert problem['reason'] == diagnostics[0]['message']
    assert problem['location_scope'] == 'field'


def test_missing_section_reports_root_field_not_an_invented_row():
    problem, = _problems({}, [{'message': '缺少properties数组', 'path': ['properties']}])
    assert problem['path'] == ['properties']
    assert problem['section'] == 'properties'
    assert 'index' not in problem
    assert problem['targets'] == [{'section': 'properties'}]


def test_unlocated_global_constraint_is_explicit_about_scope():
    problem, = _problems({}, [{'message': '提交整体结构冲突'}])
    assert problem['path'] == []
    assert problem['location_scope'] == 'operation'
    assert problem['location_note']


def test_legacy_identity_maps_to_original_unsorted_slot():
    raw = {'nodes': [{'name': '后披露'}, {'name': '先披露'}]}
    problem, = _problems(raw, [{'object_id': '先披露', 'message': '缺少证据引文'}])
    assert problem['path'] == ['nodes', 1, 'quotes']
    assert problem['reference'] == '@2'
