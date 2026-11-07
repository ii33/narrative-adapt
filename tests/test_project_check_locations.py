from narrative.project_check import _locate


def test_static_record_error_has_record_location_and_source_coordinates():
    record = {'id': 'PC1', 'kind': 'property_change', 'node_id': 'E1',
              'evidence': {'source_id': 'SRC1', 'start': 2, 'quote': '发现'}}
    node = {'id': 'E1', 'kind': 'node', 'name': '发现差异'}
    result = _locate({'code': 'VALUE', 'message': '属性值类型不符', 'object_id': 'PC1'},
                     {'E1': node}, {'PC1': record}, {'SRC1': {'title': '样本'}})
    assert result['path'] == ['property_records', 'PC1']
    assert result['reason'] == '属性值类型不符'
    assert result['target']['record_id'] == 'PC1'
    assert (result['source_id'], result['start'], result['end']) == ('SRC1', 2, 4)


def test_static_node_error_preserves_producer_field_location():
    issue = {'code': 'REF', 'message': '人物引用不存在', 'object_id': 'E1',
             'path': ['objects', 'E1', 'participants', 0]}
    result = _locate(issue, {'E1': {'kind': 'node', 'id': 'E1', 'name': '会面'}}, {}, {})
    assert result['path'] == issue['path']
    assert result['location_scope'] == 'field'


def test_project_wide_check_does_not_invent_a_repair_field():
    result = _locate({'code': 'ORDER', 'message': '来源顺序整体冲突'}, {}, {}, {})
    assert result['path'] == []
    assert result['target']['kind'] == 'project'
    assert result['location_scope'] == 'operation'
