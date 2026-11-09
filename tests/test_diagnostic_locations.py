"""Content diagnostics point to original input without guessing repair fields."""
import copy

import pytest
from agent_fixtures import project as project
from pydantic import BaseModel, ValidationError

from narrative.business_staging import _problem
from narrative.content_errors import ContentGuardError, ContentValidationError
from narrative.diagnostic_locations import normalize_problem


def test_preserves_exact_tokens_and_related_metadata_without_mutating_producer():
    raw = {'message': '引用的节点缺少引文', 'path': ['nodes', 4, 'location'],
        'related_paths': [['nodes', 2, 'quotes']], 'fields': ['location'], 'object_id': '@5'}
    before = copy.deepcopy(raw)
    row = normalize_problem(raw, operation='submit_extraction', target={'source_id': 'SRC1'})
    assert row['path'] == ['nodes', 4, 'location']
    assert row['location'] == '$["nodes"][4]["location"]'
    assert row['related_paths'] == [['nodes', 2, 'quotes']]
    assert row['reason'] == raw['message'] and row['location_scope'] == 'field'
    assert row['target'] == {'source_id': 'SRC1'} and raw == before


def test_unlocated_cross_field_error_is_explicitly_operation_wide():
    row = _problem(ContentValidationError('布局之间相互冲突'),
        operation='save_compile_settings', arguments={'settings': {}}, target={})[0]
    assert row['path'] == [] and row['location'] == '$'
    assert row['location_scope'] == 'operation' and '整个提交' in row['location_note']
    assert row['message'] == '布局之间相互冲突'


def test_pydantic_original_argument_location_is_kept():
    class Input(BaseModel):
        settings: dict[str, int]
    arguments = {'settings': {'font_size_pt': 'bad'}}
    with pytest.raises(ValidationError) as caught:
        Input.model_validate(arguments)
    row = _problem(caught.value, arguments=arguments, operation='save_compile_settings')[0]
    assert row['path'] == ['settings', 'font_size_pt']
    assert row['model_location'] == row['path']
    assert row['message'] and row['location_scope'] == 'field'


def test_unbound_model_local_location_does_not_claim_wrong_operation_field():
    class Model(BaseModel):
        title: int
    with pytest.raises(ValidationError) as caught:
        Model.model_validate({'title': 'bad'})
    row = _problem(caught.value, arguments={'payload': {'title': 'bad'}})[0]
    assert row['path'] == [] and row['model_location'] == ['title']
    assert row['location_scope'] == 'operation'


def test_operations_staging_read_and_patch_share_field_location(project):
    before = project.load_compile_settings()
    staged = project.save_compile_settings(before['settings'] | {'font_size_pt': -1}, before['revision'], '检查位置')
    problem = staged['problems'][0]
    assert problem['path'] == ['settings', 'font_size_pt']
    assert problem['location'] == '$["settings"]["font_size_pt"]'
    assert problem['operation'] == 'save_compile_settings'
    assert problem['reason'] and project.load_compile_settings() == before
    assert project.read_content_submission(staged['submission_id'])['problems'] == staged['problems']
    fixed = project.patch_content_submission(staged['submission_id'], 1,
        [{'op': 'set', 'path': problem['path'], 'value': 14}], '只修字号')
    assert fixed['status'] == 'accepted' and fixed['problems'] == []


def test_material_title_check_has_public_input_location_and_fixed_target(project):
    text = '林岚。'
    source = project.import_source('资料测试', text)
    accepted = project.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '林岚', 'quotes': [text]}], 'properties': []})
    assert accepted['status'] == 'accepted'
    person = next(n['id'] for n in project.graph_snapshot()['nodes'] if n['name'] == '林岚')
    staged = project.material_add_card(person, '', '正文保留')
    problem = staged['problems'][0]
    assert problem['path'] == ['title'] and problem['location'] == '$["title"]'
    assert problem['target'] == {'node_id': person}
    assert problem['reason'] == '资料标题不能为空'
    assert project.read_content_submission(staged['submission_id'])['problems'] == staged['problems']


def test_normalization_does_not_reclassify_guard_as_repairable(project):
    from narrative.business_staging import invoke
    def denied(_arguments):
        raise ContentGuardError('固定目标已失效')
    with pytest.raises(ContentGuardError, match='固定目标'):
        invoke(project, 'save_project_style', {'text': '内容', 'expected_revision': 0, 'reason': '目标测试'}, denied)
    assert project.list_content_submissions()['submissions'] == []


@pytest.mark.parametrize(('patch', 'field'), [
    ({'op': 'invalid'}, 'op'), ({'op': 'clear'}, 'value'),
    ({'value_type': []}, 'value_type'), ({'cardinality': 'wrong'}, 'cardinality'),
    ({'symmetric': 1}, 'symmetric'), ({'quote': ''}, 'quote'),
    ({'quote': '原文里没有这句话'}, 'quote'),
])
def test_property_checks_report_public_argument_not_generated_record_path(project, patch, field):
    text = '林岚在会议室宣布延期。'
    source = project.import_source('属性位置', text)
    result = project.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '林岚', 'quotes': [text]},
        {'type': 'location', 'name': '会议室', 'quotes': ['会议室']},
        {'type': 'scene', 'name': '会议延期', 'quotes': [text]},
        {'type': 'event', 'name': '宣布延期', 'scene': '@3', 'location': '@2', 'quotes': [text],
         'participants': ['@1']},
    ], 'properties': []})
    assert result['status'] == 'accepted'
    nodes = {n['name']: n['id'] for n in project.graph_snapshot()['nodes']}
    arguments = {'node_id': nodes['林岚'], 'property_id': '测试字段', 'event_id': nodes['宣布延期'],
        'op': 'set', 'value': '测试', 'value_type': 'string',
        'expected_revision': project.graph_project_info()['revision'], 'reason': '字段诊断'} | patch
    staged = project.record_property_change(**arguments)
    assert staged['status'] == 'staged'
    assert staged['problems'][0]['path'] == [field]
    if field == 'value':
        assert staged['problems'][0]['related_paths'] == [['op']]


def test_compile_cross_field_location_survives_model_validator_wrapper(project):
    before = project.load_compile_settings()
    settings = copy.deepcopy(before['settings'])
    settings['page_margins_mm'].update(left=80, right=80)
    staged = project.save_compile_settings(settings, before['revision'], '缩进位置')
    assert staged['status'] == 'staged'
    problem = next(p for p in staged['problems'] if 'dialogue' in p['path'])
    assert problem['path'] == ['settings', 'paragraphs', 'dialogue', 'indent_left_mm']
    assert problem['related_paths'] == [
        ['settings', 'paragraphs', 'dialogue', 'indent_right_mm'],
        ['settings', 'page_margins_mm', 'left'], ['settings', 'page_margins_mm', 'right']]
    assert project.load_compile_settings() == before


def test_creative_overrides_report_each_unknown_or_wrong_typed_field(project):
    script = project.create_screenplay('创作约定诊断', reason='位置测试')['screenplay']['id']
    before = project.load_creative_agreement(script)
    staged = project.save_creative_agreement(before['project_defaults'], before['project_revision'], '检查覆盖字段',
        screenplay_id=script, expected_script_revision=before['script_revision'],
        script_overrides={'tone': 12, 'unsupported': '错误字段'})
    assert staged['status'] == 'staged'
    assert {tuple(p['path']) for p in staged['problems']} == {
        ('script_overrides', 'tone'), ('script_overrides', 'unsupported')}
    assert all(p['message'] and p['location_scope'] == 'field' for p in staged['problems'])
    assert project.load_creative_agreement(script) == before
