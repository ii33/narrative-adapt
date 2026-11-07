"""One isolated selected-shot asset analysis task; no host repair or retry."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import accept_screenplay as qa
from accept_optional_screenplay_usage import seed as base_seed
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

TEXT = '林岚在档案室门口收起黑色折叠伞。她身穿灰色雨衣。另一镜是空走廊。'
TASK = '''请只分析本任务指定镜头尚待确定的出场元素并保存需求列表，不建立、修改或自动选择资产。读取read_storyboard_assets_context，依据选中镜头画面与正文识别人、具体形象需求和物品；本镜有林岚的灰色雨衣形象及黑色折叠伞。通过submit_storyboard_assets提交未确定元素，每项仅kind/name/usage，不填asset_id，由程序生成待确定状态。已有confirmed_elements由程序保留，不重复提交、不更改绑定。资产管理目录必须完全不变，不调用save_asset，不为元素创建提取事实。使用实际合同与修订和正文依据版本。保留第二镜和选中镜头其他字段，只保存选中镜头需求列表。不生成图片、声音、视频或导演台，不改提取、改编、剧本、批准状态。遇内容诊断按字段局部修补，不重发整稿。完成后自然结束。'''


def screenplay_content(document):
    """Exclude derived task/UI status; retain all authored story material."""
    return {'timeline': deepcopy(document['timeline']), 'segments': [
        {'id': segment['id'], 'title': segment['title'], 'units': [
            {key: deepcopy(unit.get(key)) for key in ('id', 'kind', 'enabled', 'plan', 'blocks')}
            for unit in segment['units']]} for segment in document['segments']]}


def seed(ops):
    fixture = base_seed(ops)
    ctx = ops.read_scene_table_context(fixture['segment_id'])
    table = ops.submit_scene_table(fixture['segment_id'], [{'plan': {'title': '雨衣与折叠伞', 'summary': TEXT}}],
        ctx['expected_outline_revision'], ctx['span_fingerprint'], ctx['context_fingerprint'], reason='隔离资产分析场表')
    unit = table['units'][0]['id']
    ctx = ops.read_script_unit_context(unit)
    body = ops.submit_script_unit(unit, [{'type': 'action', 'payload': {'text': TEXT}}],
        ctx['expected_plan_revision'], ctx['expected_body_revision'], ctx['span_fingerprint'],
        ctx['context_fingerprint'], ctx['plan_fingerprint'], reason='隔离资产分析正文')
    block = body['blocks'][0]['id']
    confirmed = ops.save_asset({'category': 'location', 'title': '档案室门口测试环境', 'detail': '用户已手动确定的背景对象'})
    board = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '收伞', 'description': '林岚身穿灰色雨衣，在档案室门口收起黑色折叠伞。',
         'source_block_ids': [block], 'duration_ms': 3000, 'visual_elements': [
             {'kind': 'set', 'name': '档案室门口', 'usage': '已手动确定的背景', 'asset_id': confirmed['id']}]},
        {'op': 'create_shot', 'title': '空走廊', 'description': '空走廊，画面内无人。',
         'source_block_ids': [block], 'duration_ms': 2000}])
    return fixture | {'unit_id': unit, 'selected_shot_ids': [board['shots'][0]['id']],
        'protected_shot_id': board['shots'][1]['id'], 'board_before': board,
        'assets_before': all_assets(ops), 'asset_revision_before': ops.list_assets('location')['revision'],
        'screenplay_content_before': screenplay_content(ops.read_screenplay(fixture['screenplay_id']))}


def target(fixture):
    return {'kind': 'storyboard_assets', 'ids': [fixture['unit_id']],
        'shot_ids': fixture['selected_shot_ids'], 'screenplay_id': fixture['screenplay_id']}


def all_assets(ops):
    return [card for domain in ('adaptation', 'extraction') for category in ('appearance', 'item', 'location', 'material')
        for card in ops.list_assets(category, domain=domain)['assets']]


def assess(board, fixture, assets):
    selected = set(fixture['selected_shot_ids'])
    original = {shot['id']: shot for shot in fixture['board_before']['shots']}
    current = {shot['id']: shot for shot in board['shots']}
    rows = [row for shot in board['shots'] if shot['id'] in selected for row in shot.get('visual_elements', [])]
    pending = [row for row in rows if not row.get('asset_id')]
    person = [row for row in pending if row.get('kind') == 'person' and '林岚' in row.get('name', '')]
    umbrella = [row for row in pending if row.get('kind') == 'prop' and '伞' in row.get('name', '')]
    def stable(shot):
        return {key: value for key, value in shot.items() if key not in {'visual_elements', 'assets_analyzed', 'asset_elements', 'asset_elements_status', 'asset_elements_diagnostics', 'asset_elements_missing_count', 'asset_elements_image_pending_count'}}

    return {
        'same_two_shots_and_order': [shot['id'] for shot in board['shots']] == list(original),
        'unselected_shots_unchanged': all(current.get(identifier) == shot for identifier, shot in original.items() if identifier not in selected),
        'only_selected_elements_changed': all(identifier in current and stable(current[identifier]) == stable(original[identifier]) for identifier in selected),
        'asset_library_completely_unchanged': assets == fixture['assets_before'],
        'confirmed_bindings_preserved': all(row in current.get(identifier, {}).get('visual_elements', []) for identifier in selected for row in original[identifier].get('visual_elements', []) if row.get('asset_id')),
        'new_elements_not_automatically_bound': bool(pending) and all(row.get('asset_id') is None or row in original[identifier].get('visual_elements', []) for identifier in selected for row in current.get(identifier, {}).get('visual_elements', [])),
        'person_has_specific_appearance_requirement': bool(person) and all('雨衣' in row.get('usage', '') + row.get('name', '') for row in person),
        'prop_requirement_is_unbound': bool(umbrella) and all(row.get('asset_id') is None for row in umbrella),
        'all_selected_shots_analyzed': all(current.get(identifier, {}).get('visual_elements') and current[identifier].get('assets_analyzed') is True for identifier in selected),
        'unconfirmed_elements_show_pending': all(any(view.get('name') == row['name'] and view.get('kind') == row['kind'] and view.get('status') == 'unbound' for identifier in selected for view in current.get(identifier, {}).get('asset_elements', [])) for row in pending) and bool(pending),
        'shared_scene_content_unchanged': all(board.get(key) == fixture['board_before'].get(key) for key in ('fps', 'objects', 'markers', 'sound_intents')),
        'draft_not_approved': board.get('approved') is False,
        'no_generated_media': all(not shot.get('keyframes') and not shot.get('video') for shot in board['shots']),
    }


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [*root.glob('src/narrative/storyboard*.py'), root/'src/narrative/asset_operations.py',
        root/'src/narrative/screenplay_writing.py', root/'src/narrative/screenplay_tools.py',
        root/'src/narrative/prompt_defaults.py', root/'src/narrative/qml/StoryboardPage.qml',
        root/'src/narrative/qml/AssetsPage.qml', Path(__file__).resolve()]
    report['shot_assets_implementation'] = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths if path.is_file()}
    (directory/'shot-assets-implementation.json').write_text(json.dumps(report['shot_assets_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    board = ops.read_storyboard(fixture['unit_id'])
    cards = all_assets(ops)
    checks = assess(board, fixture, cards)
    checks['authored_screenplay_unchanged'] = screenplay_content(screenplay) == fixture['screenplay_content_before']
    checks['source_unchanged'] = ops.source(fixture['source_id']) == fixture['source_before']
    checks['local_timeline_unchanged'] = ops.read_screenplay_timeline(fixture['screenplay_id']) == fixture['timeline_before']
    checks['asset_library_revision_unchanged'] = ops.list_assets('location')['revision'] == fixture['asset_revision_before']
    checks['no_content_pending'] = not ops.list_content_submissions()['submissions']
    check = ops.check_storyboard(fixture['unit_id'])
    checks['storyboard_static_ok'] = check['ok'] and not any(row.get('severity') == 'error' for row in check.get('diagnostics', []))
    directory = Path(report['audit_directory'])
    audit = json.loads((directory/'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(path.read_text(encoding='utf-8')) for path in sorted(directory.glob('metadata-*.json'))]
    sessions = {row['session_id'] for row in metadata if row.get('session_id')}
    paths = [path for path in (directory.parents[1]/'home'/'sessions').rglob('session.v4.jsonl') if path.parent.name in sessions]
    sdk_rows = [json.loads(line) for path in paths for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(sdk_rows)
    steps = [step for step in audit.get('steps', []) if step.get('ok') is True and step.get('tool') in
        {'submit_storyboard_assets', 'patch_content_submission'} and isinstance(step.get('result'), dict)
        and step['result'].get('status') not in {'staged', 'rejected'}]
    checks['sdk_and_audit_writes_match'] = bool(steps) and all(any(
        response['tool'] == step['tool'] and response['is_error'] is False and not response['binding_diagnostics']
        and same_json(response['args'], step['args']) and len(response['values']) == 1
        and same_json(response['values'][0], step['result']) for response in responses) for step in steps)
    checks['no_asset_creation_or_automatic_binding_tools'] = not any(step.get('tool') in {'save_asset', 'apply_storyboard_changes', 'delete_asset'} for step in audit.get('steps', []))
    checks['model_analysis_payload_has_no_asset_ids'] = all(set(row) == {'kind', 'name', 'usage'} for step in audit.get('steps', []) if step.get('tool') == 'submit_storyboard_assets' for item in step.get('args', {}).get('elements', []) for row in item.get('visual_elements', []))
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['shot_assets_evidence'] = {'board': board, 'assets': cards, 'check': check, 'accepted_steps': steps,
        'sdk_responses': responses, 'semantic_review_required': 'Only selected shot receives unbound raincoat appearance and folding umbrella requirements. Confirmed background binding and asset library remain unchanged; user creates or selects assets later.'}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'selected-shot-analysis-only-real/2'
    qa.FIXTURE_ORIGIN = 'New isolated two-shot screenplay. One selected shot analyzed without asset creation or automatic binding; one preconfirmed background binding must survive. No host corrections or retries; existing extraction fixture is controlled input, not a model extraction result.'
    return qa.main(validate=validate, target_factory=target, on_started=record_implementation,
        required_tools=['read_storyboard_assets_context', 'submit_storyboard_assets'],
        prompt_factory=lambda fixture: TASK+'\nunit_id='+fixture['unit_id']+'\nshot_ids='+json.dumps(fixture['selected_shot_ids']))


if __name__ == '__main__':
    raise SystemExit(main())
