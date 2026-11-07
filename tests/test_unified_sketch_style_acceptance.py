"""Offline validation of real-call evidence; stubs never count as real acceptance."""
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('unified_sketch_qa', scripts/'accept_unified_sketch_style.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prompt_checks_effective_style_simple_color_and_contact(acceptance):
    style = '本项目的独特素描呈现规范'
    actual = '统一草图呈现规范：\n'+style+'\n本次简彩\n'+acceptance.PROMPT
    assert all(acceptance.assess_prompt(style, actual, acceptance.PROMPT).values())
    assert not acceptance.assess_prompt(style+'changed', actual, acceptance.PROMPT)['effective_style_full_text_forwarded']
    assert not acceptance.assess_prompt(style, actual+'本次使用黑白灰素描', acceptance.PROMPT)['simple_color_not_forced_black_and_white']
    assert not acceptance.assess_prompt(style, actual.replace('本次简彩', ''), acceptance.PROMPT)['simple_color_not_forced_black_and_white']
    assert not acceptance.assess_prompt(style, actual.replace(acceptance.PROMPT, ''), acceptance.PROMPT)['specific_panel_request_forwarded']


def sample(tmp_path):
    from PIL import Image
    guide, actual = tmp_path/'guide.png', tmp_path/'actual.png'
    Image.new('RGB', (96, 54), 'white').save(guide)
    image = Image.new('RGB', (96, 54), 'gray')
    image.putpixel((10, 10), (20, 60, 70))
    image.save(actual)
    before = {'approved': False, 'shots': [{'id': 'shot-1', 'panels': [{'id': 'panel-1', 'candidates': []}]},
        {'id': 'shot-2', 'description': 'protected'}]}
    after = deepcopy(before)
    after['shots'][0]['panels'][0].update(candidates=[{'path': str(actual), 'simple_color': True,
        'generation_basis': {'sketch_style': 'actual style'}, 'generation_prompt': 'actual prompt'}], adopted_candidate_id=None)
    fixture = {'shot_id': 'shot-1', 'panel_id': 'panel-1', 'effective_style': 'actual style'}
    calls = [{'prompt': 'actual prompt', 'saved_guide': str(guide)}]
    return before, after, fixture, calls, {'provider': 'ComfyUI'}


def test_receipt_requires_real_candidate_and_preserved_style(acceptance, tmp_path):
    assert all(acceptance.assess_receipt(*sample(tmp_path)).values())


@pytest.mark.parametrize('mutation,key', [('calls', 'one_actual_provider_function_call'),
    ('receipt', 'provider_receipt_recorded'), ('style', 'candidate_keeps_effective_style'),
    ('color', 'candidate_retains_simple_color'), ('prompt', 'candidate_keeps_actual_generation_prompt'),
    ('guide', 'provider_output_not_internal_guide'), ('approved', 'not_automatically_adopted_or_approved'),
    ('adopted', 'not_automatically_adopted_or_approved'), ('other', 'other_shot_unchanged'),
    ('empty', 'one_real_generated_candidate')])
def test_evidence_rejects_substitution_or_missing_real_image(acceptance, tmp_path, mutation, key):
    before, after, fixture, calls, receipt = sample(tmp_path)
    candidate = after['shots'][0]['panels'][0]['candidates'][0]
    if mutation == 'calls':
        calls.append(deepcopy(calls[0]))
    elif mutation == 'receipt':
        receipt['provider'] = 'internal geometric renderer'
    elif mutation == 'style':
        candidate['generation_basis']['sketch_style'] = 'stale'
    elif mutation == 'color':
        candidate['simple_color'] = False
    elif mutation == 'prompt':
        candidate['generation_prompt'] = 'other'
    elif mutation == 'guide':
        Path(candidate['path']).write_bytes(Path(calls[0]['saved_guide']).read_bytes())
    elif mutation == 'approved':
        after['approved'] = True
    elif mutation == 'adopted':
        after['shots'][0]['panels'][0]['adopted_candidate_id'] = 'candidate-1'
    elif mutation == 'other':
        after['shots'][1]['description'] = 'changed'
    elif mutation == 'empty':
        after['shots'][0]['panels'][0]['candidates'] = []
    assert not acceptance.assess_receipt(before, after, fixture, calls, receipt)[key]


def test_offline_failure_capture_does_not_claim_generated_image(acceptance, tmp_path, monkeypatch):
    from narrative import storyboard_style_provider as provider
    calls = []
    def controlled_failure(guide, prompt, copies_to, reference_images=()):
        calls.append({'guide': Path(guide).read_bytes(), 'prompt': prompt})
        raise ConnectionRefusedError('offline controlled failure; not a real service request')
    monkeypatch.setattr(provider, 'render_sketch_image', controlled_failure)
    monkeypatch.setattr('sys.argv', ['accept_unified_sketch_style.py', '--output', str(tmp_path/'offline'), '--seconds', '1'])
    assert acceptance.main() == 1
    report = json.loads((tmp_path/'offline'/'report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'not_completed'
    assert len(calls) == report['image_operation_calls'] == len(report['provider_function_calls']) == 1
    assert report['new_model_requests'] == 0
    assert report['image_jobs_submitted'] == 0
    assert report['error_type'] == 'ConnectionRefusedError'
    assert report['checks']['failed_generation_preserves_board']
    assert report['checks']['effective_style_full_text_forwarded']
    assert report['checks']['simple_color_not_forced_black_and_white']
    assert (tmp_path/'offline'/'actual-guide-1.png').is_file()
    assert report['fixture']['effective_style'] in (tmp_path/'offline'/'actual-prompt-1.txt').read_text(encoding='utf-8')
