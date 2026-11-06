"""Source registration isolation and unchanged Operations forwarding contracts."""
from types import SimpleNamespace

import pytest

from narrative import source_tools
from narrative.tool_api import TOOLS, Tool, ToolError, _content_receipt, _need

NAMES = ('list_sources', 'read_source', 'locate_source', 'list_source_slices',
         'read_source_slice', 'create_source_slices', 'preview_pattern_slices',
         'create_pattern_slices', 'import_source')


def test_isolated_registration_matches_shared_catalog():
    registered = {}
    def register(tool):
        assert tool.name not in registered
        registered[tool.name] = tool
    for function in (source_tools.register_list_sources, source_tools.register_reading_tools,
                     source_tools.register_import_source):
        function(Tool, register, error_type=ToolError, need=_need, content_receipt=_content_receipt)
    source_tools.finalize_source_parameters(registered)
    assert tuple(registered) == NAMES
    for name, tool in registered.items():
        shared = TOOLS[name]
        assert (tool.summary, tool.schema(), tool.writes, tool.required, tool.scope) == (
            shared.summary, shared.schema(), shared.writes, shared.required, shared.scope)
    names = list(TOOLS)
    assert names.index('list_sources') < names.index('property_categories') < names.index('read_source')
    assert names.index('import_source') < names.index('preview_extraction')


def call(name, ops, **params):
    return TOOLS[name].handler(ops, params)


def test_source_reads_and_import_forward_exact_arguments():
    calls = []
    ops = SimpleNamespace(
        sources=lambda: [{'id': 'SRC1', 'title': 'Title', 'length': 4}],
        source=lambda sid: {'id': sid, 'title': 'Title', 'text': '甲😀乙丙'},
        locate=lambda sid, quotes, **kw: calls.append((sid, quotes, kw)) or ['match'],
        source_slices=lambda sid: [sid],
        source_slice=lambda sid, item: {'source_id': sid, 'id': item},
        import_source=lambda title, text: calls.append((title, text)) or 'SRC2')
    assert call('list_sources', ops)['sources'][0]['chars'] == 4
    assert call('read_source', ops, start=1, length=2)['text'] == '😀乙'
    quotes = ['甲']
    assert call('locate_source', ops, quotes=quotes, limit_per_quote=7)['results'] == ['match']
    assert calls[-1] == ('SRC1', quotes, {'limit_per_quote': 7})
    assert call('list_source_slices', ops)['slices'] == ['SRC1']
    assert call('read_source_slice', ops, slice_id=3)['id'] == '3'
    assert call('import_source', ops, title=3, text=4) == {'status': 'accepted', 'source_id': 'SRC2'}
    assert calls[-1] == ('3', '4')


@pytest.mark.parametrize('response', [
    {'status': 'staged', 'submission_id': 'submission-1', 'problems': [{'field': 'start'}]},
    {'status': 'accepted', 'submission_id': 'submission-1', 'result': [{'id': 'slice-1'}]},
    [{'id': 'slice-1'}],
])
def test_slice_receipts_and_payload_identity(response):
    rows = [{'title': 'Part', 'start': 0, 'end': 1}]
    def create(sid, actual):
        assert sid == 'SRC1' and actual is rows
        return response
    result = call('create_source_slices', SimpleNamespace(create_source_slices=create),
                  source_id='SRC1', slices=rows)
    if isinstance(response, dict) and response['status'] == 'staged':
        assert result is response
    elif isinstance(response, dict):
        assert result == {'source_id': 'SRC1', 'slices': response['result'], **_content_receipt(response)}
    else:
        assert result == {'status': 'created', 'source_id': 'SRC1', 'slices': response}


@pytest.mark.parametrize('name', ['preview_pattern_slices', 'create_pattern_slices'])
def test_pattern_forwarding_and_compact_samples(name):
    seen = []
    def operation(**kw):
        seen.append(kw)
        return {'slices': list(range(8)), 'count': 8}
    ops = SimpleNamespace(**{name: operation})
    result = call(name, ops, source_id='SRC1', pattern='^章', start_index=2, limit=3)
    assert seen == [{'source_id': 'SRC1', 'pattern': '^章', 'start_index': 2, 'limit': 3}]
    assert result == {'count': 8, 'sampled': True, 'sample_count': 6, 'slice_samples': [0, 1, 2, 5, 6, 7]}


@pytest.mark.parametrize('name,params,method', [
    ('list_source_slices', {}, 'source_slices'),
    ('read_source_slice', {'slice_id': 'missing'}, 'source_slice'),
    ('create_source_slices', {'slices': []}, 'create_source_slices'),
    ('preview_pattern_slices', {'pattern': '^章'}, 'preview_pattern_slices'),
    ('create_pattern_slices', {'pattern': '^章'}, 'create_pattern_slices'),
])
def test_operations_errors_keep_message_and_cause(name, params, method):
    error = ValueError('具体业务错误')
    def fail(*args, **kwargs):
        raise error
    with pytest.raises(ToolError, match='具体业务错误') as caught:
        call(name, SimpleNamespace(**{method: fail}), source_id='SRC1', **params)
    assert caught.value.__cause__ is error


def test_source_resolution_and_missing_parameters_are_unchanged():
    for sources in ([], [{'id': 'SRC1'}, {'id': 'SRC2'}]):
        with pytest.raises(ToolError, match='须明确提供真实SRC编号'):
            call('read_source', SimpleNamespace(sources=lambda sources=sources: sources, source=lambda sid: {}))
    with pytest.raises(ToolError, match='source_id须为真实原文编号'):
        call('read_source', SimpleNamespace(source=lambda sid: {}), source_id=3)
    with pytest.raises(ToolError, match='缺少参数：quotes'):
        call('locate_source', None, source_id='SRC1')
