"""New host capabilities inherit discovery scope and safe edit audit projection."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from narrative import tool_api
from narrative.assistant_audit import sanitize_host_audit
from narrative.tool_loading import resolve_tool_names, tool_family


@pytest.mark.parametrize('name', ['run_host_process', 'run_host_script', 'edit_host_file', 'future_host_capability'])
def test_registered_host_scope_defines_family_without_name_table(monkeypatch, name):
    tool = replace(tool_api.TOOLS['read_host_file'], name=name)
    monkeypatch.setitem(tool_api.TOOLS, name, tool)
    assert tool_family(name) == 'host'
    assert name in resolve_tool_names(families=['host'], access_mode='full')
    with pytest.raises(ValueError, match='当前可用业务组不存在'):
        resolve_tool_names(families=['host'], access_mode='standard')


@pytest.mark.parametrize('argument_field', ['args', 'arguments', 'arguments_json'])
@pytest.mark.parametrize('as_json', [False, True])
def test_edit_audit_omits_private_text_but_execution_arguments_stay_identical(argument_field, as_json):
    args = {'path': 'example.txt', 'old_text': '旧内容\n秘密甲', 'new_text': '新内容\n秘密乙', 'expected_matches': 1}
    raw = json.dumps(args, ensure_ascii=False) if as_json else args
    event = {'tool': 'edit_host_file', argument_field: raw, 'raw_response': 'PRIVATE RAW',
             'argument_fragments': ['PRIVATE FRAGMENT']}
    original = deepcopy(event)
    safe = sanitize_host_audit(event)
    assert event == original
    projected = json.loads(safe[argument_field]) if as_json else safe[argument_field]
    assert projected['path'] == args['path'] and projected['expected_matches'] == 1
    for field in ('old_text', 'new_text'):
        assert field not in projected
        assert projected[field + '_omitted'] is True
        assert projected[field + '_bytes'] == len(args[field].encode('utf-8'))
        assert len(projected[field + '_sha256']) == 64
    assert 'PRIVATE' not in json.dumps(safe) and '秘密' not in json.dumps(safe, ensure_ascii=False)
    assert sanitize_host_audit(safe) == safe


def test_write_contract_is_unchanged_and_process_payload_is_not_removed():
    safe = sanitize_host_audit({'tool': 'write_host_file', 'args': {'path': 'a', 'content': '甲'}})
    assert safe['args']['content_omitted'] is True and safe['args']['content_bytes'] == 3
    for name in ('run_host_process', 'run_host_script'):
        event = {'tool': name, 'args': {'program': 'x', 'script': 'print(1)'}, 'raw': 'receipt'}
        assert sanitize_host_audit(event) == event
