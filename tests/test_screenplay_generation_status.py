"""Only actual persisted successful body-writing tasks enable usage highlighting."""
import json

import pytest

from narrative.screenplay_generation_status import _read_task, generation_status


@pytest.fixture
def state():
    return {'script_units': {'unit-1': {'segment_id': 'segment-1'},
                             'unit-2': {'segment_id': 'segment-2'}},
            'script_segments': {'segment-1': {'screenplay_id': 'screenplay-1'},
                                'segment-2': {'screenplay_id': 'screenplay-2'}},
            'script_blocks': {'block-1': {'unit_id': 'unit-1'}, 'block-2': {'unit_id': 'unit-2'}}}


def task(root, number, status='completed', screenplay='screenplay-1', written=True):
    path = root / f'{number:02d}-task' / 'task.json'
    path.parent.mkdir(parents=True)
    row = {'status': status, 'writing_task': {'target': {'screenplay_id': screenplay}},
           'submitted_unit_ids': ['unit-1'] if written else []}
    path.write_bytes(json.dumps(row).encode('utf-8'))
    return path, row


def test_no_task_is_not_inferred_complete_from_existing_body(tmp_path, state):
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'not_generated'


def test_completed_state_survives_reopening_without_ui_memory(tmp_path, state):
    task(tmp_path, 1)
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'
    _read_task.cache_clear()
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'


@pytest.mark.parametrize('status', ['running', 'cancelled', 'incomplete', 'failed'])
def test_latest_unfinished_or_failed_task_overrides_old_success(tmp_path, state, status):
    task(tmp_path, 1)
    task(tmp_path, 2, status=status)
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == status


def test_scene_table_only_completion_does_not_claim_body_generation(tmp_path, state):
    task(tmp_path, 1, written=False)
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'not_generated'


def test_another_script_task_does_not_override_current_script(tmp_path, state):
    task(tmp_path, 1)
    task(tmp_path, 2, status='running', screenplay='screenplay-2', written=False)
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'


def test_headless_body_write_resolves_owner_without_page_target(tmp_path, state):
    path, _ = task(tmp_path, 1, written=False)
    row = {'status': 'completed', 'steps': [
        {'tool': 'submit_script_unit', 'ok': True, 'args': {'unit_id': 'unit-1'},
         'result': {'status': 'accepted'}}]}
    path.write_bytes(json.dumps(row).encode('utf-8'))
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'
    assert generation_status(tmp_path, state, 'screenplay-2')['status'] == 'not_generated'


def test_generic_running_task_conservatively_suspends_old_highlight(tmp_path, state):
    task(tmp_path, 1)
    path, _ = task(tmp_path, 2, written=False)
    path.write_bytes(b'{"status":"running"}')
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'running'


def test_old_unrelated_running_task_does_not_hide_new_success(tmp_path, state):
    path, _ = task(tmp_path, 1, written=False)
    path.write_bytes(b'{"status":"running"}')
    task(tmp_path, 2)
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'


@pytest.mark.parametrize('content', [b'{', b'[]', b'{}', b'{"status":"completed","writing_task":"bad"}'])
def test_unreadable_or_malformed_record_never_reuses_old_success(tmp_path, state, content):
    task(tmp_path, 1)
    path, _ = task(tmp_path, 2)
    path.write_bytes(content)
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'unknown'


def test_unit_target_without_redundant_screenplay_id_is_recognized(tmp_path, state):
    path, row = task(tmp_path, 1)
    row['writing_task'] = {'target': {'kind': 'unit', 'ids': ['unit-1']}}
    path.write_bytes(json.dumps(row).encode('utf-8'))
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'


def test_body_must_still_exist(tmp_path, state):
    task(tmp_path, 1)
    state['script_blocks'] = {}
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'not_generated'


def test_record_cache_tracks_final_status_update(tmp_path, state):
    path, row = task(tmp_path, 1, status='running')
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'running'
    row['status'] = 'completed'
    path.write_bytes(json.dumps(row).encode('utf-8'))
    assert generation_status(tmp_path, state, 'screenplay-1')['status'] == 'completed'
