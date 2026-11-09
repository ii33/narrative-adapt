"""Shared project sketch presentation; provider stubs verify transport and invalidation."""
from pathlib import Path

import pytest
from PIL import Image
from test_screenplay_source_references import prepared as prepared
from test_storyboard_assets import board as board

from narrative.prompt_settings import bind_prompt_snapshot, prompt_snapshot
from narrative.storyboard_sketch_style import current_sketch_style
from narrative.tool_api import TOOLS


def panels(ops, unit, b, count=1):
    shot = b['shots'][0]['id']
    result = ops.apply_storyboard_changes(unit, b['revision'], [{'op': 'create_panel', 'shot_id': shot, 'offset_ms': i * 100, 'intent': '手持道具，视线朝向入口'} for i in range(count)])
    return shot, result['shots'][0]['panels'], ops.read_storyboard(unit)


def provider(monkeypatch, seen, callback=None):
    def render(guide, prompt, copies_to, reference_images=()):
        seen.append(prompt)
        if callback:
            callback()
        Image.new('RGB', (32, 18), 'white').save(copies_to)
        return {'provider': 'stub-only', 'job_id': 'test-style'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)


def test_shared_default_and_custom_style_reach_both_image_entrypoints(board, monkeypatch):
    ops, _script, unit, b = board
    defaults = current_sketch_style(ops)
    assert all(text in defaults for text in ['以线条为主', '线', '几何体', '白底', '受光面', '软光', '固有深浅', '排线', '颜色'])
    ops.save_prompt_override('sketch_style', '统一测试：自然清楚节制，保留侧窗软光与接触暗部。', 0, '测试工程统一草图风格')
    style = current_sketch_style(ops)
    seen = []
    provider(monkeypatch, seen)
    shot, rows, b = panels(ops, unit, b)
    frame = ops.generate_storyboard_sketch(unit, shot, 0, {}, b['revision'], simple_color=False)
    result = ops.generate_storyboard_panel(unit, shot, rows[0]['id'], frame['revision'], simple_color=True)
    assert len(seen) == 2 and all(style in text for text in seen)
    assert '黑白灰' in seen[0] and '本次简彩' in seen[1]
    assert '不能把草图改成精细插画' in seen[0] and '参考板布局' in seen[1]
    candidate = result['shots'][0]['panels'][0]['candidates'][0]
    assert candidate['simple_color'] is True
    assert candidate['generation_basis']['sketch_style'] == style
    assert candidate['generation_prompt'] == seen[1]
    assert Path(candidate['path']).is_file()


def test_current_style_ignores_frozen_chat_snapshot_and_invalidates_both_outputs(board, monkeypatch):
    ops, _script, unit, b = board
    seen = []
    provider(monkeypatch, seen)
    shot, rows, b = panels(ops, unit, b)
    frame = ops.generate_storyboard_sketch(unit, shot, 0, {}, b['revision'])
    generated = ops.generate_storyboard_panel(unit, shot, rows[0]['id'], frame['revision'])
    frozen = prompt_snapshot(ops)
    ops.save_prompt_override('sketch_style', '新的草图呈现规范：保留软光。', 0, '更改生成呈现')
    with bind_prompt_snapshot(ops, frozen):
        assert current_sketch_style(ops) == '新的草图呈现规范：保留软光。'
        check = ops.check_storyboard(unit)
    assert any(row['code'] == 'sketch_basis_changed' for row in check['diagnostics'])
    assert any(row['code'] == 'panel_basis_changed' for row in check['diagnostics'])
    assert ops.read_storyboard(unit)['revision'] == generated['revision']


@pytest.mark.parametrize('entry', ['frame', 'panel'])
def test_style_change_during_generation_rejects_old_write(board, monkeypatch, entry):
    ops, _script, unit, b = board
    shot, rows, b = panels(ops, unit, b)
    seen = []
    provider(monkeypatch, seen, lambda: ops.save_prompt_override('sketch_style', '生成途中改变的草图风格', 0, '模拟并发风格调整'))
    with pytest.raises(ValueError, match='依据已变化'):
        if entry == 'frame':
            ops.generate_storyboard_sketch(unit, shot, 0, {}, b['revision'])
        else:
            ops.generate_storyboard_panel(unit, shot, rows[0]['id'], b['revision'])
    current = ops.read_storyboard(unit)
    assert current['revision'] == b['revision']
    assert current['shots'][0]['keyframes'] == []
    assert current['shots'][0]['panels'][0]['candidates'] == []


def test_unrelated_prompt_edit_does_not_invalidate_image_or_reject_write(board, monkeypatch):
    ops, _script, unit, b = board
    shot, rows, b = panels(ops, unit, b)
    seen = []
    provider(monkeypatch, seen, lambda: ops.save_prompt_override('storyboard_assets', '只分析元素的另一版说明', 0, '无关提示改变'))
    result = ops.generate_storyboard_panel(unit, shot, rows[0]['id'], b['revision'])
    assert result['shots'][0]['panels'][0]['candidates']
    assert not any(row['code'] == 'panel_basis_changed' for row in ops.check_storyboard(unit)['diagnostics'])


def test_batch_simple_color_default_and_explicit_setting(board, monkeypatch):
    ops, _script, unit, b = board
    shot, rows, b = panels(ops, unit, b, count=2)
    seen = []
    provider(monkeypatch, seen)
    first = ops.generate_storyboard_panel(unit, shot, rows[0]['id'], b['revision'])
    result = ops.generate_storyboard_panels(unit, shot, first['revision'], missing_only=True, simple_color=True)
    assert len(seen) == 2
    candidates = [row['candidates'][0] for row in result['shots'][0]['panels']]
    assert [candidate['simple_color'] for candidate in candidates] == [False, True]
    assert '本次使用黑白灰' in candidates[0]['generation_prompt']
    assert '本次简彩' in candidates[1]['generation_prompt']


@pytest.mark.parametrize('entry', ['frame', 'panel', 'batch'])
def test_simple_color_requires_boolean_before_provider_request(board, monkeypatch, entry):
    ops, _script, unit, b = board
    shot, rows, b = panels(ops, unit, b)
    def fail(*args, **kwargs):
        pytest.fail('invalid options must not contact provider')
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', fail)
    with pytest.raises(ValueError):
        if entry == 'frame':
            ops.generate_storyboard_sketch(unit, shot, 0, {}, b['revision'], simple_color=1)
        elif entry == 'panel':
            ops.generate_storyboard_panel(unit, shot, rows[0]['id'], b['revision'], simple_color=1)
        else:
            ops.generate_storyboard_panels(unit, shot, b['revision'], simple_color=1)


def test_all_image_tools_publish_same_simple_color_option():
    for name in ('generate_storyboard_sketch', 'generate_storyboard_panel', 'generate_storyboard_panels'):
        assert TOOLS[name].params['simple_color']['type'] == 'boolean'



def test_panel_simple_color_metadata_follows_adoption_into_keyframe(board, monkeypatch):
    ops, _script, unit, b = board
    shot, rows, b = panels(ops, unit, b)
    seen = []
    provider(monkeypatch, seen)
    result = ops.generate_storyboard_panel(unit, shot, rows[0]['id'], b['revision'], simple_color=True)
    candidate = result['shots'][0]['panels'][0]['candidates'][0]
    final = ops.apply_storyboard_changes(unit, result['revision'], [
        {'op': 'adopt_panel_candidate', 'shot_id': shot, 'panel_id': rows[0]['id'], 'candidate_id': candidate['id']},
        {'op': 'use_panel_frame', 'shot_id': shot, 'panel_id': rows[0]['id']}])
    assert final['shots'][0]['keyframes'][0]['sketch_source'] == {'source': 'panel', 'panel_id': rows[0]['id'], 'candidate_id': candidate['id'], 'simple_color': True}



def test_global_line_drawing_default_applies_without_project_override(prepared):
    from narrative.prompt_defaults import DEFAULT_PROMPTS
    from narrative.storyboard_sketch_style import sketch_render_instructions
    ops, _script, _segment, _events = prepared
    row = next(row for row in ops.load_prompt_settings()['prompts'] if row['id'] == 'sketch_style')
    assert row['overridden'] is False
    assert row['effective'] == DEFAULT_PROMPTS['sketch_style']['text']
    assert row['version'] == '3'
    for simple_color in (False, True):
        prompt = sketch_render_instructions(row['effective'], simple_color)
        assert '几何体' in prompt and '白底' in prompt
        assert '必要局部背光' in prompt or '既定光照' in prompt
        assert ('排线' in prompt or '灰调' in prompt) and ('纸纹' in prompt or '材质细节' in prompt)
    assert '简彩' in sketch_render_instructions(row['effective'], True)
