"""The live acceptance must prove exact diagnostic repair, not just final row counts."""
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('diagnostic_qa', scripts / 'accept_diagnostic_locations.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def repair(module):
    original = module.original_draft()
    current = deepcopy(original)
    current['nodes'][0]['quotes'] = ['林岚']
    current['properties'][0]['value'] = '暂停'
    step = {'tool': 'patch_extraction_submission', 'ok': True,
        'args': {'submission_id': 'extraction-submission-qa', 'patches': [
            {'op': 'set', 'section': 'nodes', 'index': 0, 'field': 'quotes', 'value': ['林岚']},
            {'op': 'set', 'section': 'properties', 'index': 0, 'field': 'value', 'value': '暂停'},
        ]}, 'result': {'status': 'accepted', 'submission_status': 'committed', 'validation_status': 'valid'}}
    return {'original_draft': original, 'submission_id': 'extraction-submission-qa'}, current, [step]


def test_exact_original_field_paths_and_reasons_required(acceptance):
    rows = [{'severity': 'error', 'path': list(path), 'message': '字段缺失'} for path in acceptance.EXPECTED_PATHS]
    assert all(acceptance.assess_initial_diagnostics(rows).values())
    rows[0]['path'] = ['nodes', 9, 'quotes']
    assert not acceptance.assess_initial_diagnostics(rows)['both_original_error_fields_located']


@pytest.mark.parametrize('missing', ['path', 'message'])
def test_blank_or_unlocated_errors_cannot_pass(acceptance, missing):
    rows = [{'path': list(path), 'message': '缺少引文'} for path in acceptance.EXPECTED_PATHS]
    rows.append({'path': ['properties', 0, 'op'], 'message': '无效操作'})
    rows[-1].pop(missing)
    assert not acceptance.assess_initial_diagnostics(rows)['all_errors_have_path_and_reason']


def test_exact_single_repair_passes(acceptance):
    fixture, current, steps = repair(acceptance)
    checks, accepted = acceptance.assess_repair(steps, fixture, current)
    assert all(checks.values()) and len(accepted) == 1


def test_unrelated_field_change_fails(acceptance):
    fixture, current, steps = repair(acceptance)
    current['nodes'][4]['name'] = '擅改事件'
    checks, _ = acceptance.assess_repair(steps, fixture, current)
    assert not checks['only_reported_fields_changed']


def test_full_resubmission_or_multiple_patches_fail(acceptance):
    fixture, current, steps = repair(acceptance)
    checks, _ = acceptance.assess_repair(steps + deepcopy(steps), fixture, current)
    assert not checks['single_patch_automatically_committed']
    checks, _ = acceptance.assess_repair(steps + [{'tool': 'submit_extraction', 'ok': True}], fixture, current)
    assert not checks['no_full_resubmission']


def test_wrong_submission_or_tool_error_fails(acceptance):
    fixture, current, steps = repair(acceptance)
    steps[0]['args']['submission_id'] = 'another-submission'
    steps[0]['ok'] = False
    checks, _ = acceptance.assess_repair(steps, fixture, current)
    assert not checks['repair_target_preserved']
    assert not checks['no_tool_errors']
    assert not checks['single_patch_automatically_committed']


def test_task_does_not_disclose_missing_field_answers(acceptance):
    assert 'quotes' not in acceptance.TASK and 'properties' not in acceptance.TASK
    assert '发布状态' not in acceptance.TASK and '暂停' not in acceptance.TASK
