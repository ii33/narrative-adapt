"""A legacy staged draft's extra root key is repairable without replacing the authored draft.

`U-10` 以后，新提交的草稿在准备阶段就把未知顶层字段丢弃了，所以带这种字段的暂存稿只可能是
**升级前留下的**。这里按当时的形状直接写一份暂存稿：patch 的「删除单个未知顶层字段」必须
还能修它，否则旧工程里的待修稿只能整份重交。
"""
import json
from copy import deepcopy

import pytest
from agent_fixtures import project as project

from narrative.content_errors import ContentConflictError
from narrative.extraction_staging import _load, _save
from narrative.tool_api import TOOLS

LEGACY_ID = 'extraction-submission-1'


def draft():
    return {'nodes': [{'type': 'person', 'name': '甲'}],
            'properties': [], 'result_note': '仅用于说明的多余字段'}


def staged(project, raw=None):
    """升级前留下的暂存稿：草稿里还带着未知顶层字段。"""
    source = project.import_source('局部修补测试', '甲。乙。')
    raw = draft() if raw is None else raw
    with project.project.store.connect(write=True) as db:
        _save(db, {'submission_id': LEGACY_ID, 'submission_revision': 1,
                   'submission_status': 'staged', 'source_id': source, 'start': 0, 'end': 4,
                   'formal_revision': project.graph_project_info()['revision'],
                   'result': deepcopy(raw), 'validation_status': 'invalid',
                   'problems': [{'severity': 'error', 'code': 'DRAFT_SCHEMA',
                                 'path': ['result_note'],
                                 'message': '未知草稿字段：result_note', 'targets': []}],
                   'counts': {'nodes': len(raw.get('nodes') or []), 'properties': 0},
                   'last_reason': '升级前留下的待修稿'})
        stored = _load(db, LEGACY_ID)
    assert stored['submission_status'] == 'staged' and 'result_note' in json.dumps(stored, ensure_ascii=False)
    return source, {'submission_id': LEGACY_ID, 'submission_revision': 1, 'submission_status': 'staged'}


def test_remove_unknown_root_rechecks_and_commits_without_changing_original_or_target(project):
    raw = draft()
    before = deepcopy(raw)
    source, submission = staged(project, raw)
    result = project.patch_extraction_submission(submission['submission_id'], 1,
        [{'op': 'remove', 'path': ['result_note']}], '删除合同外的说明字段')
    assert result['status'] == 'accepted' and result['submission_status'] == 'committed'
    assert result['submission_revision'] == 2
    assert (result['source_id'], result['start'], result['end']) == (source, 0, 4)
    assert raw == before
    rows = project.read_extraction_submission(submission['submission_id'], section='nodes')['entries']
    assert [row['item'] for row in rows] == before['nodes']
    assert [n['name'] for n in project.graph_snapshot()['nodes']] == ['甲']
    assert project.list_extraction_submissions()['submissions'] == []


@pytest.mark.parametrize('field', ['nodes', 'properties', 'uncertainties', 'exclusions'])
def test_known_root_sections_cannot_be_removed(project, field):
    _, submission = staged(project)
    before = project.read_extraction_submission(submission['submission_id'])
    with pytest.raises(ValueError, match='不能删除正式草稿字段'):
        project.patch_extraction_submission(submission['submission_id'], 1,
            [{'op': 'remove', 'path': [field]}], '非法删根')
    assert project.read_extraction_submission(submission['submission_id']) == before
    assert not project.graph_snapshot()['nodes']


@pytest.mark.parametrize('patch', [
    {'op': 'set', 'path': ['result_note'], 'value': '替换'},
    {'op': 'replace', 'path': ['result_note'], 'value': '替换'},
    {'op': 'remove', 'path': []},
    {'op': 'remove', 'path': ['nodes', 0, 'name']},
    {'op': 'remove', 'path': [0]},
    {'op': 'remove', 'path': 'result_note'},
    {'op': 'remove', 'path': ['result_note'], 'section': 'nodes'},
])
def test_invalid_root_patch_shape_is_atomic(project, patch):
    _, submission = staged(project)
    before = project.read_extraction_submission(submission['submission_id'])
    with pytest.raises(ValueError, match='仅支持remove'):
        project.patch_extraction_submission(submission['submission_id'], 1, [patch], '错误补丁')
    assert project.read_extraction_submission(submission['submission_id']) == before
    assert not project.graph_snapshot()['nodes']


def test_later_invalid_patch_does_not_save_earlier_root_or_row_change(project):
    _, submission = staged(project)
    before = project.read_extraction_submission(submission['submission_id'], section='nodes')
    with pytest.raises(ValueError, match='不存在'):
        project.patch_extraction_submission(submission['submission_id'], 1, [
            {'op': 'set', 'section': 'nodes', 'index': 0, 'field': 'name', 'value': '更名'},
            {'op': 'remove', 'path': ['result_note']},
            {'op': 'remove', 'path': ['missing']},
        ], '错误后续补丁')
    assert project.read_extraction_submission(submission['submission_id'], section='nodes') == before
    assert not project.graph_snapshot()['nodes']


def test_removing_illegal_target_copy_does_not_change_bound_source_range(project):
    raw = draft() | {'source_id': '伪原文', 'start': 999, 'end': 1000, 'revision': -1}
    source, submission = staged(project, raw)
    result = project.patch_extraction_submission(submission['submission_id'], 1,
        [{'op': 'remove', 'path': [key]} for key in ('result_note', 'source_id', 'start', 'end', 'revision')],
        '只删除result内部的非法副本')
    assert result['status'] == 'accepted', result
    assert (result['source_id'], result['start'], result['end']) == (source, 0, 4)


def test_stale_revision_still_rejects_root_removal(project):
    _, submission = staged(project)
    # 删掉必填字段让这份稿子仍然是 invalid：它留在 staged、修订号往前走。
    changed = project.patch_extraction_submission(submission['submission_id'], 1,
        [{'op': 'remove', 'section': 'nodes', 'index': 0, 'field': 'name'}], '删掉必填名称')
    assert changed['status'] == 'staged' and changed['submission_revision'] == 2
    with pytest.raises(ContentConflictError):
        project.patch_extraction_submission(submission['submission_id'], 1,
            [{'op': 'remove', 'path': ['result_note']}], '过期补丁')


def test_tool_schema_publishes_only_restricted_unknown_root_remove():
    variants = TOOLS['patch_extraction_submission'].params['patches']['items']['oneOf']
    root, = [spec for spec in variants if 'path' in spec['properties']]
    assert root['additionalProperties'] is False
    assert root['properties']['op']['const'] == 'remove'
    path = root['properties']['path']
    assert path['minItems'] == path['maxItems'] == 1
    assert set(path['items']['not']['enum']) == {'nodes', 'properties', 'uncertainties', 'exclusions'}
