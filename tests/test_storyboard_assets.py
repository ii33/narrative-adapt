"""Asset analysis uses real asset objects and immutable per-shot targets."""
import pytest
from test_screenplay_source_references import prepared as prepared
from test_screenplay_source_references import submit
from test_screenplay_writing import body

from narrative.content_errors import ContentConflictError
from narrative.operations import Operations
from narrative.screenplay_writing import validate_script_writing_tool_call
from narrative.tool_api import TOOLS


@pytest.fixture
def board(prepared):
    ops, script, segment, _events = prepared
    scene = submit(ops, segment, [{'title': '站前', 'summary': '甲在车站等候。'}])['units'][0]
    unit = scene['id']
    block = body(ops, unit, [{'type': 'action', 'payload': {'text': '甲拿着一把伞。'}}])['blocks'][0]['id']
    ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '等候', 'source_block_ids': [block], 'visual_elements': [{'kind': 'person', 'name': '甲', 'usage': '站在门边', 'asset_id': None}]},
        {'op': 'create_shot', 'title': '空景', 'source_block_ids': [block]}])
    return ops, script, unit, ops.read_storyboard(unit)


def row(shot, asset=None, kind='person', name='甲'):
    result = {'shot_id': shot, 'visual_elements': [{'kind': kind, 'name': name, 'usage': '出场'}]}
    if asset is not None:
        result['visual_elements'][0]['asset_id'] = asset
    return result


def call(ops, unit, b, entries, shots=None):
    return ops.submit_storyboard_assets(unit, shots or [b['shots'][0]['id']], b['revision'], ops.read_script_unit_context(unit)['unit']['body_revision'], entries)


def test_analysis_only_replaces_pending_preserves_confirmed_without_guessing_identity(board, tmp_path):
    ops, _script, unit, b = board
    before = ops.graph_snapshot()
    person = ops.save_asset({'category': 'appearance', 'title': '形象2'})
    first = b['shots'][0]
    bound = {'kind': 'person', 'name': '甲', 'usage': '用户确认', 'asset_id': person['id']}
    ops.apply_storyboard_changes(unit, b['revision'], [{'op': 'update_shot', 'shot_id': first['id'], 'visual_elements': [bound, {'kind': 'prop', 'name': '旧需求', 'usage': '', 'asset_id': None}]}])
    b = ops.read_storyboard(unit)
    before_assets = ops.list_assets('appearance')
    entry = row(first['id'], kind='prop', name='雨伞')
    # Same name intentionally remains separate; program never guesses identity.
    entry['visual_elements'].append({'kind': 'person', 'name': '甲', 'usage': '不同形象需求'})
    result = call(ops, unit, b, [entry])
    assert result['submission_status'] == 'committed'
    now = ops.read_storyboard(unit)
    elements = now['shots'][0]['visual_elements']
    assert elements[0] == bound
    assert elements[1:] == [dict(v, asset_id=None) for v in entry['visual_elements']]
    assert now['shots'][1] == b['shots'][1]
    assert now['shots'][0]['asset_elements'][0]['label'] == '甲 - 形象2'
    assert now['shots'][0]['asset_elements_missing_count'] == 2
    assert ops.list_assets('appearance') == before_assets
    assert ops.graph_snapshot() == before
    assert Operations.open(tmp_path / 'sources.nproj').read_storyboard(unit)['shots'] == now['shots']


@pytest.mark.parametrize('extra', [None, 'appearance-999', [], {}])
def test_analysis_cannot_choose_assets_and_content_can_patch_to_requirements(board, extra):
    ops, _script, unit, b = board
    entry = row(b['shots'][0]['id'])
    entry['visual_elements'][0]['asset_id'] = extra
    staged = call(ops, unit, b, [entry])
    assert staged['status'] == 'staged'
    assert staged['problems'][0]['path'] == ['elements', 0, 'visual_elements', 0]
    assert ops.read_storyboard(unit) == b
    fixed = ops.patch_content_submission(staged['submission_id'], staged['submission_revision'],
        [{'op': 'remove', 'path': ['elements', 0, 'visual_elements', 0, 'asset_id']}], '移除自动选择')
    assert fixed['submission_status'] == 'committed'
    assert ops.read_storyboard(unit)['shots'][0]['visual_elements'][0]['asset_id'] is None


def test_selection_coverage_empty_results_and_stale_revision(board):
    ops, _script, unit, b = board
    ids = [s['id'] for s in b['shots']]
    staged = call(ops, unit, b, [{'shot_id': ids[0], 'visual_elements': []}], ids)
    assert staged['status'] == 'staged'
    assert staged['problems'][0]['path'] == ['elements']
    fixed = ops.patch_content_submission(staged['submission_id'], staged['submission_revision'],
        [{'op': 'append', 'path': ['elements'], 'value': {'shot_id': ids[1], 'visual_elements': []}}], '补齐空镜')
    assert fixed['submission_status'] == 'committed'
    assert all(s['asset_elements_status'] == 'empty' for s in ops.read_storyboard(unit)['shots'])
    with pytest.raises(ContentConflictError):
        call(ops, unit, b, [])


def test_task_freezes_selected_shots_and_body_and_refuses_other_writes(board):
    ops, script, unit, b = board
    ids = [b['shots'][0]['id']]
    task = ops.prepare_script_writing_task({'kind': 'storyboard_assets', 'ids': [unit], 'shot_ids': ids, 'screenplay_id': script})
    args = {'unit_id': unit, 'shot_ids': ids, 'revision': b['revision'], 'expected_body_revision': task['expected_body_revision'], 'elements': []}
    validate_script_writing_tool_call(task, 'submit_storyboard_assets', args, ops)
    for name, bad in [('save_asset', {'payload': {'category': 'item', 'title': '伞'}}), ('delete_asset', {'asset_id': 'item-1'}), ('apply_storyboard_changes', {'unit_id': unit}), ('submit_storyboard_assets', args | {'shot_ids': [b['shots'][1]['id']]}),
                      ('submit_storyboard_assets', args | {'expected_body_revision': 999})]:
        with pytest.raises(ValueError):
            validate_script_writing_tool_call(task, name, bad, ops)
    context = ops.read_storyboard_assets_context(unit, ids)
    assert context['shot_ids'] == ids and len(context['shots']) == 1
    assert {'read_storyboard_assets_context', 'submit_storyboard_assets'} <= TOOLS.keys()


def test_body_change_is_conflict_not_pending_content(board):
    ops, _script, unit, b = board
    old = ops.read_script_unit_context(unit)['unit']['body_revision']
    body(ops, unit, [{'type': 'action', 'payload': {'text': '甲转身。'}}])
    with pytest.raises(ContentConflictError, match='正文'):
        ops.submit_storyboard_assets(unit, [b['shots'][0]['id']], b['revision'], old, [])


def test_read_view_reminds_missing_without_guessing_semantics(board):
    ops, _script, unit, b = board
    view = ops.read_storyboard(unit)
    first, second = view['shots']
    assert first['asset_elements'][0]['status'] == 'unbound'
    assert first['asset_elements_missing_count'] == 1
    assert second['asset_elements_status'] == 'unanalysed'
    check = ops.check_storyboard(unit)
    missing = next(d for d in check['diagnostics'] if d['code'] == 'ASSET_ELEMENT_UNBOUND')
    assert missing['path'] == ['shots', 0, 'visual_elements', 0, 'asset_id']
    assert any(d['code'] == 'ASSET_ELEMENTS_UNANALYSED' for d in check['diagnostics'])


def test_patch_cannot_replace_fixed_target_or_original_body_basis(board):
    ops, _script, unit, b = board
    staged = call(ops, unit, b, [row(b['shots'][0]['id'], 'forbidden')])
    for path, value in [(['shot_ids'], [b['shots'][1]['id']]), (['expected_body_revision'], 999),
                        (['elements', 0, 'shot_id'], b['shots'][1]['id'])]:
        with pytest.raises(ValueError):
            ops.patch_content_submission(staged['submission_id'], staged['submission_revision'],
                [{'op': 'set', 'path': path, 'value': value}], '不能换目标')
    assert ops.read_storyboard(unit) == b


def test_manual_asset_selection_uses_normal_binding_checks(board):
    ops, _script, unit, b = board
    shot = b['shots'][0]['id']
    asset = ops.save_asset({'category': 'item', 'title': '独立雨伞'})
    wrong = ops.apply_storyboard_changes(unit, b['revision'], [{'op': 'update_shot', 'shot_id': shot, 'visual_elements': [{'kind': 'person', 'name': '甲', 'usage': '', 'asset_id': asset['id']}]}])
    assert wrong['status'] == 'staged'
    assert wrong['problems'][0]['path'] == ['changes', 0, 'visual_elements', 0, 'asset_id']
    good = ops.apply_storyboard_changes(unit, b['revision'], [{'op': 'update_shot', 'shot_id': shot, 'visual_elements': [{'kind': 'prop', 'name': '伞', 'usage': '', 'asset_id': asset['id']}]}])
    assert good['submission_status'] == 'committed'
    with pytest.raises(ValueError, match='分镜引用'):
        ops.delete_asset(asset['id'])



def test_explicit_create_missing_is_per_row_atomic_and_empty(board):
    ops, _script, unit, b = board
    first, second = [shot['id'] for shot in b['shots']]
    call(ops, unit, b, [row(first), row(second)], [first, second])
    before = ops.graph_snapshot()
    now = ops.read_storyboard(unit)
    created = ops.create_missing_storyboard_assets(unit, [first, second], now['revision'])
    assert created['created_count'] == 2 and created['status'] == 'accepted'
    assert len(created['committed_changes']) == 3
    assert len({card['id'] for card in created['created_assets']}) == 2
    assert all(card['category'] == 'appearance' and not card['node_id'] and not card['detail'] and not card['prompt'] and not card['images'] and not card['reference_images'] for card in created['created_assets'])
    assert created['board']['shots'][0]['visual_elements'][0]['usage'] == '出场'
    assert all(shot['asset_elements'][0]['status'] == 'image_missing' for shot in created['board']['shots'])
    assert ops.graph_snapshot() == before
    again = ops.create_missing_storyboard_assets(unit, [first, second], created['board']['revision'])
    assert again['created_count'] == 0 and len(again['skipped']) == 2
    assert again['status'] == 'unchanged' and again['committed_changes'] == []
    assert again['board']['revision'] == created['board']['revision']
    with pytest.raises(ContentConflictError):
        ops.create_missing_storyboard_assets(unit, [first], now['revision'])


def test_explicit_create_row_selector_and_asset_revision(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    entry = row(first)
    entry['visual_elements'].append({'kind': 'prop', 'name': '伞', 'usage': '手持'})
    call(ops, unit, b, [entry])
    current = ops.read_storyboard(unit)
    asset_revision = ops.read_storyboard_assets_context(unit, [first])['asset_revision']
    with pytest.raises(ContentConflictError):
        ops.create_missing_storyboard_assets(unit, [first], current['revision'], asset_revision + 1)
    created = ops.create_missing_storyboard_assets(unit, [first], current['revision'], asset_revision,
                                                  [{'shot_id': first, 'index': 1}])
    assert created['created_count'] == 1
    assert created['created_assets'][0]['category'] == 'item'
    assert created['board']['shots'][0]['visual_elements'][0]['asset_id'] is None
    assert created['bindings'][0]['index'] == 1
    assert 'create_missing_storyboard_assets' in TOOLS


def test_explicit_create_failure_rolls_back_asset_cards_and_bindings(board, monkeypatch):
    from narrative.asset_operations import AssetOperationsMixin
    ops, _script, unit, b = board
    ids = [shot['id'] for shot in b['shots']]
    call(ops, unit, b, [row(identifier) for identifier in ids], ids)
    current = ops.read_storyboard(unit)
    before = ops.read_storyboard_assets_context(unit, ids)
    original = AssetOperationsMixin.save_asset
    written = []
    def save(self, *args, **kwargs):
        if written:
            raise OSError('injected asset write failure')
        value = original(self, *args, **kwargs)
        written.append(value)
        return value
    monkeypatch.setattr(AssetOperationsMixin, 'save_asset', save)
    with pytest.raises(OSError, match='injected'):
        ops.create_missing_storyboard_assets(unit, ids, current['revision'])
    after = ops.read_storyboard_assets_context(unit, ids)
    assert after['assets'] == before['assets'] and after['asset_revision'] == before['asset_revision']
    assert ops.read_storyboard(unit) == current


@pytest.mark.parametrize('refs', [[{'shot_id': 'missing', 'index': 0}], [{'shot_id': 'shot-1', 'index': True}], []])
def test_explicit_create_refuses_bad_row_targets_without_creating_cards(board, refs):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    before = ops.read_storyboard_assets_context(unit, [first])
    with pytest.raises(ValueError):
        ops.create_missing_storyboard_assets(unit, [first], b['revision'], element_refs=refs)
    after = ops.read_storyboard_assets_context(unit, [first])
    assert after['assets'] == before['assets'] and after['asset_revision'] == before['asset_revision']
    assert ops.read_storyboard(unit) == b
