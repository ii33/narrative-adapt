"""外观偏好与配色令牌：三档模式、原子持久化、深浅两套值都成对。"""
from __future__ import annotations

import json

import pytest

from narrative import appearance, theme


def test_default_path_follows_env_override(tmp_path, monkeypatch):
    target = tmp_path / 'nested' / 'appearance.json'
    monkeypatch.setenv('NARRATIVE_APPEARANCE_FILE', str(target))
    assert appearance.default_path() == target


def test_missing_file_falls_back_to_system(tmp_path):
    assert appearance.load(tmp_path / 'absent.json') == 'system'


def test_saved_mode_round_trips(tmp_path):
    target = tmp_path / 'appearance.json'
    for mode in appearance.MODES:
        appearance.save(mode, target)
        assert appearance.load(target) == mode
    assert json.loads(target.read_text(encoding='utf-8')) == {'mode': 'dark'}


def test_unknown_mode_is_rejected_on_write(tmp_path):
    with pytest.raises(ValueError):
        appearance.save('sepia', tmp_path / 'appearance.json')
    assert not (tmp_path / 'appearance.json').exists()


def test_broken_file_falls_back_to_system(tmp_path):
    target = tmp_path / 'appearance.json'
    target.write_text('{"mode": "sepia"}', encoding='utf-8')
    assert appearance.load(target) == 'system'
    target.write_text('not json', encoding='utf-8')
    assert appearance.load(target) == 'system'


def test_mode_resolution_prefers_explicit_choice():
    assert appearance.is_dark('dark', system_dark=False) is True
    assert appearance.is_dark('light', system_dark=True) is False
    assert appearance.is_dark('system', system_dark=True) is True
    assert appearance.is_dark('system', system_dark=False) is False


def test_every_token_pair_defines_both_schemes():
    light = theme.tokens(False)
    dark = theme.tokens(True)
    assert set(light) == set(dark)
    assert set(theme.TOKENS) <= set(light)
    # 剧本预览纸面是有意在两套里都保持白纸黑字，其余令牌必须真的随外观变化。
    fixed = {'previewPaper', 'previewInk'}
    for name in theme.TOKENS:
        assert light[name].startswith('#') and len(light[name]) == 7
        if name not in fixed:
            assert light[name] != dark[name], name
    assert light['radius'] == dark['radius'] == theme.LAYOUT['radius']
    assert light['gap'] == dark['gap'] == theme.LAYOUT['gap']


def test_palette_colors_follow_tokens():
    for dark in (False, True):
        values = theme.tokens(dark)
        colors = theme._palette_colors(dark)
        assert colors['window'] == values['paper']
        assert colors['base'] == values['card']
        assert colors['text'] == values['ink']
        assert colors['highlight'] == values['accent']
        assert colors['highlightedText'] == values['onAccent']
        assert colors['placeholderText'] == values['muted']
