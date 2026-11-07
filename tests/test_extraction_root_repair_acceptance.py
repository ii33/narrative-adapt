"""Root-repair acceptance checks cannot pass on a wrong or wholesale repair."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('root_repair_qa', scripts / 'accept_extraction_root_repair.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(module):
    f = {'submission_id': 'extraction-submission-1', 'submission_revision': 2,
         'original_draft': module.original_draft()}
    current = copy.deepcopy(f['original_draft'])
    current.pop('result_note')
    steps = [
        {'tool': 'read_extraction_submission', 'ok': True, 'args': {'submission_id': f['submission_id']}},
        {'tool': 'patch_extraction_submission', 'ok': True, 'args': {
            'submission_id': f['submission_id'], 'expected_revision': 2, 'patches': [copy.deepcopy(module.PATCH)]},
         'result': {'status': 'accepted', 'submission_status': 'committed', 'validation_status': 'valid'}},
    ]
    return f, current, steps


def test_exact_root_removal_passes_without_mutating_fixture(acceptance):
    f, current, steps = fixture(acceptance)
    before = copy.deepcopy((f, current, steps))
    checks, accepted = acceptance.assess_repair(steps, f, current)
    assert all(checks.values()) and len(accepted) == 1
    assert (f, current, steps) == before


@pytest.mark.parametrize('mutation', ['target', 'revision', 'extra_patch', 'replace_nodes', 'two_calls', 'tool_error', 'not_committed', 'no_read', 'resubmit'])
def test_incomplete_wrong_or_extra_repairs_fail(acceptance, mutation):
    f, current, steps = fixture(acceptance)
    patch = steps[1]
    if mutation == 'target':
        patch['args']['submission_id'] = 'extraction-submission-other'
    elif mutation == 'revision':
        patch['args']['expected_revision'] = 99
    elif mutation == 'extra_patch':
        patch['args']['patches'].append({'op': 'remove', 'path': ['nodes']})
    elif mutation == 'replace_nodes':
        current['nodes'] = []
    elif mutation == 'two_calls':
        steps.append(copy.deepcopy(patch))
    elif mutation == 'tool_error':
        patch['ok'] = False
    elif mutation == 'not_committed':
        patch['result']['submission_status'] = 'staged'
    elif mutation == 'no_read':
        steps.pop(0)
    elif mutation == 'resubmit':
        steps.append({'tool': 'submit_extraction', 'ok': True})
    assert not all(acceptance.assess_repair(steps, f, current)[0].values())


def test_seed_does_not_fake_business_content(acceptance):
    raw = acceptance.original_draft()
    assert set(raw) == {'nodes', 'properties', 'result_note'}
    assert len(raw['nodes']) == 1 and raw['nodes'][0]['type'] == 'person'
    assert raw['nodes'][0]['quotes'] == [acceptance.TEXT]
    assert raw['properties'] == []
