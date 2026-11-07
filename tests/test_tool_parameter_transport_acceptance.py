"""Synthetic QA evidence built from real local Operations, never a model result."""
import codecs
import importlib.util
import json
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from narrative.assistant_host import cancel_project_host_commands, host_access_context
from narrative.operations import Operations


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('tool_parameter_transport_acceptance', scripts / 'accept_tool_parameter_transport.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence(acceptance, tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'synthetic.nproj')
    fixture = acceptance.seed(ops)
    steps = []

    def call(name, args):
        result = getattr(ops, name)(**args)
        steps.append({'tool': name, 'args': args, 'result': result, 'ok': True})
        return result

    with host_access_context('full', 'synthetic-task', threading.Event(), ops.project_info().path):
        draft = {'nodes': [{'type': 'person', 'name': '林岚', 'quotes': ['林岚']},
            {'type': 'location', 'name': '档案室', 'quotes': ['档案室']},
            {'type': 'scene', 'name': '暂停名单发布', 'quotes': [acceptance.TEXT]},
            {'type': 'event', 'name': '决定暂停名单发布', 'scene': '@3', 'participants': ['@1'],
                'location': '@2', 'quotes': [acceptance.TEXT]}], 'properties': []}
        committed = call('submit_extraction', {'source_id': fixture['source_id'], 'result': draft})
        assert committed['status'] == 'accepted', committed
        call('read_host_file', {'path': fixture['file']})
        call('edit_host_file', {'path': fixture['file'], 'old_text': '旧稿', 'new_text': '新稿✨'})
        call('read_host_file', {'path': fixture['file']})
        command = call('run_host_script', {'language': 'python',
            'script': "import sys\nsys.stdout.buffer.write(('transport-ok:' + sys.stdin.read()).encode('utf-8'))\n",
            'stdin_text': acceptance.STDIN})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            output = call('read_host_command', {'command_id': command['command_id']})
            if output['status'] != 'running':
                break
            time.sleep(.02)
        assert output['status'] == 'completed' and output['exit_code'] == 0
        assert output['content'] == 'transport-ok:' + acceptance.STDIN
    directory = tmp_path / 'qa' / 'assistant' / 'tasks' / 'synthetic-task'
    directory.mkdir(parents=True)
    session = directory.parents[1] / 'home' / 'sessions' / 'synthetic-session' / 'session.v4.jsonl'
    session.parent.mkdir(parents=True)
    rows = []
    for i, step in enumerate(steps):
        call_id = f'synthetic-call-{i}'
        rows += [{'data': {'message': {'role': 'assistant', 'content': [{'type': 'tool-call',
            'id': call_id, 'name': step['tool'], 'arguments': json.dumps(step['args'])}]}}},
            {'data': {'message': {'role': 'tool', 'toolCallId': call_id, 'isError': False,
                'content': [{'type': 'text', 'text': json.dumps(step['result'])}]}}}]
    session.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    (directory / 'metadata-1.json').write_text(json.dumps({'session_id': 'synthetic-session', 'model_requests': []}), encoding='utf-8')
    audit = {'access_mode': 'full', 'steps': steps}
    (directory / 'task.json').write_text(json.dumps(audit), encoding='utf-8')
    yield ops, fixture, {'audit_directory': str(directory)}, audit, session, rows
    cancel_project_host_commands(ops.project_info().path)


def test_seed_locks_service_and_utf16_bytes_without_network(acceptance, tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'seed.nproj')
    fixture = acceptance.seed(ops)
    settings = ops.harness_settings()
    assert (settings['provider'], settings['base_url'], settings['model'], settings['key_env'], settings['reasoning_effort']) == (
        'ark', 'https://ark.cn-beijing.volces.com/api/plan/v3', 'deepseek-v4.1-flash-economy', 'ARK_API_KEY', 'low')
    assert Path(fixture['file']).read_bytes() == codecs.BOM_UTF16_LE + acceptance.BEFORE.encode('utf-16-le')
    assert acceptance.AFTER == acceptance.BEFORE.replace('旧稿', '新稿✨')
    assert '\r\n' in acceptance.BEFORE and '\\text' in acceptance.BEFORE
    assert json.dumps(acceptance.STDIN, ensure_ascii=False) in acceptance.prompt(fixture)
    assert '\\n' in acceptance.STDIN and '\n' in acceptance.STDIN and '"' in acceptance.STDIN
    assert ops.read_assistant_access(fixture['chat_id'])['access_mode'] == 'full'


def test_validator_real_local_shortcut_text_and_stdin_via_synthetic_sdk(acceptance, evidence):
    ops, fixture, report, audit, session, rows = evidence
    checks = acceptance.validate(ops, fixture, ops.read_screenplay(fixture['screenplay_id']), [], report)
    assert all(checks.values()), checks
    records = ops.property_records()
    events = [n for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'event']
    assert len(events) == 1
    location = [r for r in records if r.get('kind') == 'property_change' and r.get('property_id') == '地点']
    assert location and all(r['event_id'] == r['node_id'] == events[0]['id'] for r in location)


@pytest.mark.parametrize('case', ['changed_args', 'missing_is_error', 'changed_stdin', 'wrong_file_bytes'])
def test_validator_cannot_certify_inconsistent_evidence(acceptance, evidence, case):
    ops, fixture, report, audit, session, source_rows = evidence
    rows = deepcopy(source_rows)
    if case == 'changed_args':
        next(row['data']['message']['content'][0] for row in rows if row['data']['message'].get('role') == 'assistant')['arguments'] = '{}'
    elif case == 'missing_is_error':
        next(row['data']['message'] for row in rows if row['data']['message'].get('role') == 'tool').pop('isError')
    elif case == 'changed_stdin':
        next(s for s in audit['steps'] if s['tool'] == 'run_host_script')['args']['stdin_text'] += 'corruption'
        (Path(report['audit_directory']) / 'task.json').write_text(json.dumps(audit), encoding='utf-8')
    else:
        Path(fixture['file']).write_bytes(b'wrong encoding and text')
    session.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    checks = acceptance.validate(ops, fixture, ops.read_screenplay(fixture['screenplay_id']), [], report)
    assert not all(checks.values()), (case, checks)
