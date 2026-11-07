"""One isolated real task verifies intent-only extraction and literal host IO."""
from __future__ import annotations

import codecs
import hashlib
import json
from collections import Counter
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results
from accept_tool_loading import usage_breakdown

TEXT = '林岚在档案室决定暂停名单发布。'
BEFORE = '旧稿\r\n第二行：C:\\资料\\text "示例"\r\n'
AFTER = BEFORE.replace('旧稿', '新稿✨')
STDIN = '中文 "引号" C:\\路径\\n字面\\n\n第二行😀'


def seed(ops):
    from narrative.chat_store import ChatStore
    from narrative.project_package import layout
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('参数精简短文', TEXT)
    script = ops.create_screenplay('验收空容器', reason='独立参数验收')['screenplay']
    chat = ChatStore(ops.project_info().path).create('工具参数传输验收')
    ops.set_assistant_access(chat['id'], 'full')
    target = layout(ops.project_info().path).entry.parent / 'transport-sandbox'
    target.mkdir()
    file = target / 'unicode.txt'
    file.write_bytes(codecs.BOM_UTF16_LE + BEFORE.encode('utf-16-le'))
    return {'source_id': source, 'screenplay_id': script['id'], 'chat_id': chat['id'], 'file': str(file),
        'source_before': ops.source(source), 'screenplay_before': ops.read_screenplay(script['id'])}


def prompt(f):
    return ('仅完成这个隔离验收，文件操作只针对给定文件，不读取私人文件或运行其他命令。'
        '读取原文source_id=' + f['source_id'] + '，提取必要人物、地点、场景和一个完整事件。'
        '事件直接填location引用，properties留空，由程序生成地点属性，不自编记录ID、序号或任务关联字段。'
        '读取文件' + f['file'] + '，通过edit_host_file将旧稿改成新稿✨，保留其余内容和原文件编码/BOM/换行，再读确认。'
        '通过run_host_script运行一次Python脚本：读取stdin原样输出并前加transport-ok:；stdin_text精确为'
        + json.dumps(STDIN, ensure_ascii=False) + '。脚本作为script文本传入，不拼shell引号、Base64或EncodedCommand。'
        '随后read_host_command确认实际完成且exit_code=0。不得重跑或启动后台服务，不修改其他业务数据。')


def validate(ops, f, screenplay, calls, report):
    from narrative.assistant_audit import sanitize_host_audit
    from narrative.event_location import event_location_issues
    graph, records = ops.graph_snapshot(), ops.property_records()
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(rows)
    good = [s for s in audit['steps'] if s.get('ok') is True]
    committed = [s for s in good if s['tool'] == 'submit_extraction' and s.get('result', {}).get('status') == 'accepted'
        and s['result'].get('submission_status') == 'committed' and s['result'].get('validation_status') == 'valid']
    direct = [s for s in committed if any(n.get('type') == 'event' and n.get('location') for n in s['args']['result']['nodes'])
        and s['args']['result']['properties'] == []]
    script_calls = [s for s in good if s['tool'] == 'run_host_script']
    reads = [s['result'] for s in good if s['tool'] == 'read_host_file']
    outputs = [s['result'] for s in good if s['tool'] == 'read_host_command']
    checked = [s for s in good if s['tool'] in {'submit_extraction', 'run_host_script', 'read_host_command', 'read_host_file', 'edit_host_file'}]
    def signature(tool, args, result):
        safe = sanitize_host_audit({'tool': tool, 'args': args})['args']
        return json.dumps({'tool': tool, 'args': safe, 'result': result}, ensure_ascii=False, sort_keys=True)
    native_counts = Counter(signature(s['tool'], s['args'], s['result']) for s in checked)
    sdk_counts = Counter(signature(r['tool'], r['args'], r['values'][0]) for r in responses
        if r['tool'] in {s['tool'] for s in checked} and r['is_error'] is False
        and not r['binding_diagnostics'] and len(r['values']) == 1)
    command_id = script_calls[0]['result'].get('command_id') if len(script_calls) == 1 else None
    place_ids = {n['id'] for n in graph['nodes'] if n['node_type'] == 'location' and n['name'] == '档案室'}
    event_ids = {n['id'] for n in graph['nodes'] if n['node_type'] == 'event'}
    ordered_edits = [r for r in responses if r['tool'] == 'edit_host_file' and r['is_error'] is False]
    ordered_reads = [r for r in responses if r['tool'] == 'read_host_file' and r['is_error'] is False
        and len(r['values']) == 1 and isinstance(r['values'][0], dict)]
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['parameter_transport_evidence'] = {'steps': checked, 'sdk_responses': responses, 'graph': graph, 'records': records,
        'file_bytes_match': Path(f['file']).read_bytes() == codecs.BOM_UTF16_LE + AFTER.encode('utf-16-le')}
    return {'full_access_task': audit.get('access_mode') == 'full',
        'intent_only_event_location_committed': len(direct) == 1 and len([n for n in graph['nodes'] if n['node_type'] == 'event']) == 1,
        'location_rule_valid': not event_location_issues(graph['objects'], records, snapshot=graph),
        'known_place_matches_source': any(r.get('kind') == 'property_change' and r.get('node_id') in event_ids
            and r.get('event_id') == r.get('node_id') and r.get('scope', 'world') == 'world'
            and r.get('value') in place_ids for r in records if isinstance(r.get('value'), str)),
        'text_encoding_bom_newlines_preserved': report['parameter_transport_evidence']['file_bytes_match'],
        'model_received_exact_updated_text': any(r.get('content') == AFTER and r.get('codec') == 'utf-16le' and r.get('bom') for r in reads),
        'read_edit_read_order': any(any(a['result_index'] < edit['call_index'] and a['values'][0].get('content') == BEFORE for a in ordered_reads)
            and any(b['call_index'] > edit['result_index'] and b['values'][0].get('content') == AFTER for b in ordered_reads) for edit in ordered_edits),
        'single_script_with_exact_stdin': len([s for s in audit['steps'] if s['tool'] == 'run_host_script']) == 1
            and len(script_calls) == 1 and script_calls[0]['args'].get('stdin_text') == STDIN
            and script_calls[0]['args'].get('language') == 'python',
        'script_completed_with_literal_unicode_output': any(r.get('status') == 'completed' and r.get('exit_code') == 0
            and r.get('command_id') == command_id and r.get('complete') is True and 'transport-ok:' + STDIN in r.get('content', '') for r in outputs),
        'sdk_and_audit_agree': bool(native_counts) and native_counts == sdk_counts,
        'source_unchanged': ops.source(f['source_id']) == f['source_before'],
        'screenplay_unchanged': screenplay == f['screenplay_before']}


def started(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    names = ['assistant_host.py', 'assistant_host_tools.py', 'host_text.py', 'host_text_operations.py',
        'model_tool_schema.py', 'extraction_draft.py', 'tool_api.py', 'tool_loading.py', 'assistant_audit.py']
    report['parameter_transport_implementation'] = {name: hashlib.sha256((root / 'src/narrative' / name).read_bytes()).hexdigest() for name in names}


def main():
    qa.seed, qa.TEXT = seed, TEXT
    qa.PROMPT_VERSION = 'tool-parameter-transport-real/1'
    qa.FIXTURE_ORIGIN = 'One synthetic sentence plus a UTF16LE BOM/CRLF file and harmless Python stdin echo; no private data, warmup or retries.'
    return qa.main(validate=validate, required_tools=['read_source', 'submit_extraction', 'read_host_file',
        'edit_host_file', 'run_host_script', 'read_host_command'], use_writing_target=False,
        allow_extraction_change=True, prompt_factory=prompt, on_started=started)


if __name__ == '__main__':
    raise SystemExit(main())
