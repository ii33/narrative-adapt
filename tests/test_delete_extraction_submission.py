"""Draft deletion is an immediate, audited command, never a deferred patch."""
import json

import pytest

from narrative import extraction_staging, mcp_server, tool_api, tool_cli
from narrative.content_errors import ContentConflictError, ContentGuardError
from narrative.content_operations import CONTENT_OPERATIONS
from narrative.operations import Operations
from narrative.project_package import layout
from narrative.tool_loading import tool_family


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'profiles.sqlite3'))
    entry = tmp_path / 'delete-draft.nproj'
    ops = Operations.create_graph_project(entry)
    source = ops.import_source('删除暂存测试', '甲。乙。')
    return ops, entry, source


def staged(ops, source):
    return ops.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '待修人物', 'quotes': ['原文没有的句子']},
    ], 'properties': []})


def test_delete_removes_only_selected_draft_preserves_facts_history_and_counter(context):
    ops, entry, source = context
    accepted = ops.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '甲', 'quotes': ['甲。']},
    ], 'properties': []})
    assert accepted['status'] == 'accepted'
    first, second = staged(ops, source), staged(ops, source)
    facts = ops.graph_snapshot(), ops.property_records()
    history = layout(entry).extraction / 'assistant' / 'tasks' / 'previous-task' / 'raw.txt'
    history.parent.mkdir(parents=True)
    history.write_text('原始模型输出', encoding='utf-8')
    result = ops.delete_extraction_submission(first['submission_id'], 1, '用户明确清除旧草稿')
    assert result == {'status': 'accepted', 'deleted_submission_id': first['submission_id'],
                      'submission_revision': 1, 'formal_revision': facts[0]['revision']}
    assert (ops.graph_snapshot(), ops.property_records()) == facts
    assert history.read_text(encoding='utf-8') == '原始模型输出'
    with pytest.raises(ValueError, match='不存在'):
        ops.read_extraction_submission(first['submission_id'])
    assert [r['submission_id'] for r in ops.list_extraction_submissions()['submissions']] == [second['submission_id']]
    with ops.project.store.connect() as db:
        action = db.execute('SELECT action,reason,summary FROM actions WHERE id=?',
                            ('delete-' + first['submission_id'],)).fetchone()
        assert db.execute('SELECT value FROM meta WHERE key=?',
                          (extraction_staging.PREFIX + first['submission_id'],)).fetchone() is None
    assert action[0] == 'delete_extraction_submission' and action[1] == '用户明确清除旧草稿'
    assert json.loads(action[2]) == {'submission_id': first['submission_id'], 'submission_revision': 1, 'source_id': source}
    third = staged(ops, source)
    assert int(third['submission_id'].rsplit('-', 1)[1]) > int(second['submission_id'].rsplit('-', 1)[1])


def test_stale_or_committed_submission_is_not_deleted(context):
    ops, _, source = context
    draft = staged(ops, source)
    with pytest.raises(ContentConflictError, match='revision'):
        ops.delete_extraction_submission(draft['submission_id'], 2, '过期删除')
    assert ops.read_extraction_submission(draft['submission_id'])['submission_revision'] == 1
    committed = ops.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '甲', 'quotes': ['甲。']},
    ], 'properties': []})
    facts = ops.graph_snapshot(), ops.property_records()
    with pytest.raises(ContentGuardError, match='入库'):
        ops.delete_extraction_submission(committed['submission_id'], 1, '禁止删除已入库状态')
    assert ops.read_extraction_submission(committed['submission_id'])['submission_status'] == 'committed'
    assert (ops.graph_snapshot(), ops.property_records()) == facts


def test_unknown_and_io_failure_do_not_become_staged_or_partially_delete(context, monkeypatch):
    ops, _, source = context
    draft = staged(ops, source)
    before = ops.list_extraction_submissions()
    before_content = ops.list_content_submissions()
    with pytest.raises(ValueError, match='不存在'):
        ops.delete_extraction_submission('extraction-submission-missing', 1, '未知草稿')
    def fail(*_args):
        raise OSError('注入审计写入错误')
    monkeypatch.setattr(extraction_staging, '_audit_deleted', fail)
    with pytest.raises(OSError, match='审计'):
        ops.delete_extraction_submission(draft['submission_id'], 1, 'IO错误不暂存')
    assert ops.list_extraction_submissions() == before
    assert ops.list_content_submissions() == before_content
    with ops.project.store.connect() as db:
        assert db.execute("SELECT count(*) FROM actions WHERE action='delete_extraction_submission'").fetchone()[0] == 0


@pytest.mark.parametrize('revision,reason', [(True, '删除'), (0, '删除'), (1, ''), (1, None)])
def test_invalid_command_arguments_have_no_side_effect(context, revision, reason):
    ops, _, source = context
    draft = staged(ops, source)
    with pytest.raises(ValueError):
        ops.delete_extraction_submission(draft['submission_id'], revision, reason)
    assert ops.read_extraction_submission(draft['submission_id'])['submission_status'] == 'staged'


@pytest.mark.parametrize('transport', ['tool', 'cli', 'mcp'])
def test_public_transports_share_immediate_deletion(context, transport, capsys):
    ops, entry, source = context
    draft = staged(ops, source)
    args = {'submission_id': draft['submission_id'], 'expected_revision': 1, 'reason': '删除明确旧暂存'}
    name = 'delete_extraction_submission'
    assert name not in CONTENT_OPERATIONS and tool_api.TOOLS[name].writes
    assert tool_family(name) == 'extraction'
    if transport == 'tool':
        result = tool_api.call(name, args, project=entry)
    elif transport == 'cli':
        assert tool_cli.main(['tool', 'call', name, json.dumps(args), '--project', str(entry), '--json']) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        response = mcp_server.handle({'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': args}}, default_project=str(entry))
        assert not response['result']['isError']
        result = json.loads(response['result']['content'][0]['text'])
    assert result['status'] == 'accepted' and result['deleted_submission_id'] == draft['submission_id']
    assert ops.list_extraction_submissions()['submissions'] == []
    assert ops.list_content_submissions()['submissions'] == []
