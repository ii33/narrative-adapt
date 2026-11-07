"""Zero-model deterministic postcheck of an explicit create-missing-assets click.

Starts from an immutable completed real analysis report, duplicates its isolated
project through Operations, and credits program-created empty cards separately
from the model's original unbound demand list. Never modifies the source report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from accept_storyboard_assets import all_assets, screenplay_content

KINDS = {'person': 'appearance', 'prop': 'item', 'set': 'location'}
DERIVED = {'visual_elements', 'asset_elements', 'asset_elements_status', 'asset_elements_diagnostics',
    'asset_elements_missing_count', 'asset_elements_image_pending_count'}


def assess_click(before, after, shot_ids, assets_before, assets_after, receipt):
    selected = set(shot_ids)
    original = {shot['id']: shot for shot in before['shots']}
    current = {shot['id']: shot for shot in after['shots']}
    old_cards = {card['id']: card for card in assets_before}
    cards = {card['id']: card for card in assets_after}
    new_cards = {key: card for key, card in cards.items() if key not in old_cards}
    pending = [(shot['id'], index, row) for shot in before['shots'] if shot['id'] in selected
        for index, row in enumerate(shot.get('visual_elements', [])) if not row.get('asset_id')]
    pairs = [(row, current.get(owner, {}).get('visual_elements', [])[index]) for owner, index, row in pending
        if index < len(current.get(owner, {}).get('visual_elements', []))]
    confirmed = [(owner, index, row) for owner in selected for index, row in enumerate(original[owner].get('visual_elements', [])) if row.get('asset_id')]
    def stable(shot):
        return {key: value for key, value in shot.items() if key not in DERIVED}
    return {
        'button_created_one_card_per_pending_element': bool(pending) and receipt.get('created_count') == len(pending) == len(new_cards),
        'all_pending_rows_bound_to_new_distinct_cards': len(pairs) == len(pending) and len({row.get('asset_id') for _, row in pairs}) == len(pending)
            and all(row.get('asset_id') in new_cards for _, row in pairs),
        'new_cards_are_empty_except_identity': bool(new_cards) and all(card.get('title') and not card.get('detail') and not card.get('prompt')
            and not card.get('images') and not card.get('reference_images') and not card.get('node_id')
            and not card.get('valid_from') and not card.get('valid_to') for card in new_cards.values()),
        'new_card_categories_match_element_kinds': bool(pairs) and all(row.get('asset_id') in cards and cards[row['asset_id']]['category'] == KINDS.get(old['kind']) for old, row in pairs),
        'element_requirements_preserved': len(pairs) == len(pending) and all({key: value for key, value in old.items() if key != 'asset_id'} == {key: value for key, value in row.items() if key != 'asset_id'} for old, row in pairs),
        'previous_confirmed_bindings_preserved': all(owner in current and index < len(current[owner]['visual_elements']) and current[owner]['visual_elements'][index] == row for owner, index, row in confirmed),
        'previous_asset_cards_unchanged': all(cards.get(key) == value for key, value in old_cards.items()),
        'same_shots_and_order': [shot['id'] for shot in after['shots']] == list(original),
        'unselected_shots_unchanged': all(current.get(key) == value for key, value in original.items() if key not in selected),
        'selected_shot_authored_content_unchanged': all(key in current and stable(current[key]) == stable(original[key]) for key in selected),
        'shared_scene_content_unchanged': all(after.get(key) == before.get(key) for key in ('approved', 'fps', 'objects', 'markers', 'sound_intents')),
        'new_cards_await_images': bool(pending) and all(any(view.get('asset_id') == row.get('asset_id') and view.get('status') == 'image_missing' for shot in after['shots'] for view in shot.get('asset_elements', [])) for _, row in pairs),
    }


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    from narrative.operations import Operations
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source_report = args.source_report.resolve()
    original_bytes = source_report.read_bytes()
    prior = json.loads(original_bytes)
    if prior.get('status') != 'passed' or prior.get('prompt_version') != 'selected-shot-analysis-only-real/2':
        raise ValueError('Source must be the completed v2 real analysis-only report; no new model task will be started')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_project = source_report.parent/'剧本闭环验收.nproj'
    source = Operations.open(source_project)
    fixture = prior['fixture']
    unit, script, shots = fixture['unit_id'], fixture['screenplay_id'], fixture['selected_shot_ids']
    def snapshot(ops):
        return {'graph': ops.graph_snapshot(), 'adaptation': ops.adaptation_snapshot(),
            'screenplay': screenplay_content(ops.read_screenplay(script)), 'board': ops.read_storyboard(unit), 'assets': all_assets(ops)}
    original = snapshot(source)
    original_task = json.loads((Path(prior['audit_directory'])/'task.json').read_text(encoding='utf-8'))
    original_evidence_files = [source_report, source_report.parent/'manifest.json', source_report.parent/'user-task.txt',
        *[path for path in Path(prior['audit_directory']).rglob('*') if path.is_file() and path.suffix in {'.json', '.jsonl', '.txt'}]]
    evidence_before = {str(path): sha(path) for path in original_evidence_files}
    current_instruction = source.prepare_script_writing_task(original_task['writing_task']['target'], prompt=original_task['writing_task']['prompt'])['instruction']
    report = {'status': 'running', 'postcheck_version': 'explicit-create-missing-assets/1',
        'started': datetime.now(UTC).isoformat(), 'source_report': str(source_report), 'source_report_sha256': sha(source_report),
        'source_real_task_status': prior['task_status'], 'source_real_model_requests': prior['model_requests'],
        'source_real_total_tokens': prior['actual_total_tokens'], 'new_model_requests': 0, 'new_model_tokens': 0,
        'simulation': 'Explicit user button click invokes deterministic Operations after the real analysis; program-created cards are not model output.',
        'automatic_model_retry': False, 'original_model_output_modified': False,
        'source_real_prompt_version': prior['prompt_version'],
        'source_real_analysis_instruction_sha256': hashlib.sha256(original_task['writing_task']['instruction'].encode()).hexdigest(),
        'current_analysis_instruction_sha256': hashlib.sha256(current_instruction.encode()).hexdigest()}
    runtime_files = {**prior['implementation_sha256'], **prior['shot_assets_implementation']}
    report['current_implementation_sha256'] = {str(path): sha(path) for name in runtime_files for path in [Path(name)]
        if path.is_file() and path.suffix in {'.py', '.qml'}}
    try:
        copied = Operations.duplicate_graph_project(source_project, output/'按钮验证副本.nproj')
        before = snapshot(copied)
        context = copied.read_storyboard_assets_context(unit, shots)
        # This is the sole deliberate business action, equivalent to the user's
        # button click. It is not a model repair or an assistant follow-up task.
        receipt = copied.create_missing_storyboard_assets(unit, shots, before['board']['revision'], context['asset_revision'])
        after = snapshot(copied)
        checks = assess_click(before['board'], after['board'], shots, before['assets'], after['assets'], receipt)
        checks.update({
            'copied_model_board_was_not_rewritten': before['board'] == prior['shot_assets_evidence']['board'],
            'copied_asset_library_matches_original_model_result': before['assets'] == prior['shot_assets_evidence']['assets'],
            'authored_screenplay_unchanged': before['screenplay'] == after['screenplay'],
            'extraction_unchanged': before['graph'] == after['graph'],
            'adaptation_unchanged': before['adaptation'] == after['adaptation'],
            'source_project_unchanged': snapshot(source) == original,
            'source_report_unchanged': source_report.read_bytes() == original_bytes,
            'original_raw_evidence_unchanged': all(path.is_file() and sha(path) == expected for name, expected in evidence_before.items() for path in [Path(name)]),
            'analysis_instruction_unchanged_since_real_task': current_instruction == original_task['writing_task']['instruction'],
            'no_staged_content': not copied.list_content_submissions()['submissions']})
        check = copied.check_storyboard(unit)
        checks['static_check_no_errors'] = check['ok'] and not any(item.get('severity') == 'error' for item in check.get('diagnostics', []))
        report.update(status='passed' if all(checks.values()) else 'not_passed', checks=checks, fixture=fixture,
            copied_project=str(output/'按钮验证副本.nproj'), before_board=before['board'], after_board=after['board'],
            before_assets=before['assets'], after_assets=after['assets'], explicit_button_receipt=receipt, static_check=check)
    except Exception as error:
        report.update(status='not_passed', error_type=type(error).__name__, error=str(error))
    report['finished'] = datetime.now(UTC).isoformat()
    with (output/'report.json').open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'status': report['status'], 'directory': str(output), 'new_model_requests': 0}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
