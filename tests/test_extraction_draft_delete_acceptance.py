"""A delete receipt alone cannot hide a wrong target or extra writes."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('draft_delete_qa', scripts / 'accept_extraction_draft_delete.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    return {'submission_id': 'extraction-submission-1', 'submission_revision': 1}


def steps():
    return [
        {'tool': 'read_extraction_submission', 'ok': True,
         'args': {'submission_id': 'extraction-submission-1'}, 'result': {}},
        {'tool': 'delete_extraction_submission', 'ok': True,
         'args': {'submission_id': 'extraction-submission-1', 'expected_revision': 1, 'reason': '清除旧草稿'},
         'result': {'status': 'accepted', 'deleted_submission_id': 'extraction-submission-1'}},
    ]


def test_exact_read_then_delete_passes(acceptance):
    checks, accepted = acceptance.assess_delete(steps(), fixture())
    assert all(checks.values()) and len(accepted) == 1


@pytest.mark.parametrize('mutation', ['wrong_target', 'wrong_revision', 'missing_reason', 'duplicate', 'error', 'repair', 'late_read'])
def test_receipt_cannot_hide_invalid_execution(acceptance, mutation):
    data = steps()
    if mutation == 'wrong_target':
        data[1]['args']['submission_id'] = 'other-draft'
    elif mutation == 'wrong_revision':
        data[1]['args']['expected_revision'] = 2
    elif mutation == 'missing_reason':
        data[1]['args']['reason'] = ''
    elif mutation == 'duplicate':
        data.append(copy.deepcopy(data[1]))
    elif mutation == 'error':
        data[1]['ok'] = False
    elif mutation == 'repair':
        data.insert(1, {'tool': 'patch_extraction_submission', 'ok': True})
    else:
        data.reverse()
    assert not all(acceptance.assess_delete(data, fixture())[0].values())


def test_missing_check_does_not_hide_unrelated_errors(acceptance):
    class Ops:
        def read_extraction_submission(self, _identifier):
            raise ValueError('权限不足')
    assert not acceptance.submission_missing(Ops(), 'draft')


def test_missing_check_does_not_hide_io_failures(acceptance):
    class Ops:
        def read_extraction_submission(self, _identifier):
            raise OSError('设备错误')
    with pytest.raises(OSError):
        acceptance.submission_missing(Ops(), 'draft')


def test_only_expected_missing_draft_counts_as_deleted(acceptance):
    class Ops:
        def read_extraction_submission(self, _identifier):
            raise ValueError('提取暂存不存在')
    assert acceptance.submission_missing(Ops(), 'draft')
