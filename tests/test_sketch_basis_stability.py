"""B3 生成依据稳定性：排演怎么变都不该判「依据已变化」。

回归的就是「出图必被拒」：以前依据里带着二维调度、机位投影、命名时刻和正在响的
声音节拍，改一个对象位置或挪一个动作关键点，已生成的帧就被判失效。现在依据只剩
稳定输入，这个文件把两边都钉住：

* 排演侧（对象位置/动作关键帧/命名时刻/声音节拍/机位）随便改——不拒绝；
* 内容侧（正文块/创作约定/项目风格/资产卡/框图/衔接帧）一改就拒绝，且报错点名键。
"""
import wave

import pytest
from PIL import Image
from test_screenplay_writing import body
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative.storyboard_art import changed_basis_keys

pytestmark = pytest.mark.slow

STABLE_KEYS = {'unit_id', 'shot_id', 'offset_ms', 'sound_notes', 'description', 'shot_language',
               'visual_elements', 'sound_intents', 'body_revision', 'sketch_style', 'blocks',
               'creative_agreement', 'project_style', 'assets', 'frame_diagram', 'handoff'}


def stub(monkeypatch, callback=None):
    def render(guide, prompt, copies_to, reference_images=()):
        if callback:
            callback()
        Image.new('RGB', (24, 14), 'gray').save(copies_to)
        return {'provider': 'stub-only', 'job_id': 'job-stable'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)


def wav(path):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\x00\x00' * 8000)
    return str(path)


def codes(ops, unit):
    return {row['code'] for row in ops.check_storyboard(unit)['diagnostics']}


def shot_with_frame(project, monkeypatch, tmp_path, duration=4000, offset=0):
    ops, unit, block = setup_board(project)
    shot = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '窗边等待', 'duration_ms': duration, 'source_block_ids': [block]}])['shots'][0]['id']
    image = tmp_path / 'sketch.png'
    Image.new('RGB', (20, 12), 'gray').save(image)
    framed = ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'],
                                          [{'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': offset, 'sketch_path': str(image)}])
    return ops, unit, block, shot, framed


def test_basis_carries_only_stable_inputs(project, monkeypatch, tmp_path):
    ops, unit, block, shot, board = shot_with_frame(project, monkeypatch, tmp_path)
    current = ops.read_storyboard(unit)
    row = next(item for item in current['shots'] if item['id'] == shot)
    basis, _references = ops._storyboard_sketch_basis(current, row, 0, ops.read_script_unit_context(unit))
    # 漂移字段一个都不在：objects／camera_view／named_moments／active_sound_cues 全部退出依据。
    assert set(basis) == STABLE_KEYS
    assert basis['blocks'] and basis['frame_diagram']['sha256']
    assert basis['handoff'] is None


def test_director_staging_edits_never_invalidate_the_frame(project, monkeypatch, tmp_path):
    ops, unit, block, shot, board = shot_with_frame(project, monkeypatch, tmp_path)
    stub(monkeypatch)
    board = ops.generate_storyboard_sketch(unit, shot, 0, {}, board['revision'])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'create_object', 'name': '甲', 'kind': 'person', 'x': .2, 'y': .8},
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200, 'moment_name': '递伞'}])
    actor = board['objects'][0]['id']
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'update_object', 'object_id': actor, 'x': .7, 'y': .3},
        {'op': 'set_action_keyframe', 'object_id': actor, 'time_ms': 0, 'x': .2, 'interpolation': 'linear'},
        {'op': 'set_action_keyframe', 'object_id': actor, 'time_ms': 1500, 'x': .9},
        {'op': 'create_marker', 'name': '递伞时刻', 'time_ms': 900},
        {'op': 'set_camera_keyframe', 'shot_id': shot, 'offset_ms': 0, 'x': .5, 'y': -1.0, 'z': .4},
        {'op': 'create_sound', 'shot_id': shot, 'kind': 'effect', 'label': '雨声', 'time_ms': 0,
         'duration_ms': 400, 'audio_path': wav(tmp_path / 'rain.wav')}])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    # 挪动作关键点、挪命名时刻（声音跟着走）：仍然不算依据变化。
    key = next(row['id'] for row in board['objects'][0]['keyframes'] if row['time_ms'] == 1500)
    marker = board['markers'][0]['id']
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'move_action_keyframe', 'object_id': actor, 'keyframe_id': key, 'time_ms': 1800},
        {'op': 'update_marker', 'marker_id': marker, 'time_ms': 1100},
        {'op': 'move_camera_keyframe', 'shot_id': shot, 'keyframe_id': board['shots'][0]['camera']['keys'][0]['id'],
         'offset_ms': 200}])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    assert ops.check_storyboard(unit)['affected_frames'] == []


def basis_of(ops, unit, shot):
    board = ops.read_storyboard(unit)
    row = next(item for item in board['shots'] if item['id'] == shot)
    return ops._storyboard_sketch_basis(board, row, 0, ops.read_script_unit_context(unit))[0]


@pytest.mark.parametrize('kind, key', [
    ('blocks', 'blocks'),
    ('project_style', 'project_style'),
    ('creative_agreement', 'creative_agreement'),
    ('assets', 'assets'),
])
def test_only_stable_input_changes_mark_the_frame(project, monkeypatch, tmp_path, kind, key):
    ops, unit, block, shot, board = shot_with_frame(project, monkeypatch, tmp_path)
    asset = ops.save_asset({'category': 'appearance', 'title': '形象甲', 'detail': '短发'})
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'create_object', 'name': '甲', 'kind': 'person', 'asset_id': asset['id']}])
    stub(monkeypatch)
    board = ops.generate_storyboard_sketch(unit, shot, 0, {}, board['revision'])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    recorded = basis_of(ops, unit, shot)
    mutate(ops, unit, block, kind, asset)
    assert 'sketch_basis_changed' in codes(ops, unit)
    # 依据变了不等于顺手改图：帧和它记录的哈希都还在原处。
    assert (ops.read_storyboard(unit)['shots'][0]['keyframes'][0]['sketch_basis_hash']
            == board['shots'][0]['keyframes'][0]['sketch_basis_hash'])
    # 改动确实落在点名的键上；正文与项目风格会连带 body_revision／创作约定一起变，
    # 这是同一份文本的两个投影，不是排演漂移。
    changed = set(changed_basis_keys(recorded, basis_of(ops, unit, shot)))
    assert key in changed and changed <= STABLE_KEYS | {'body_revision'}
    assert changed - {'body_revision', 'blocks', 'project_style', 'creative_agreement', 'assets'} == set()


def mutate(ops, unit, block, kind, asset):
    if kind == 'blocks':
        body(ops, unit, [{'id': block, 'type': 'action', 'payload': {'text': '甲走到门口。'}}])
    elif kind == 'project_style':
        ops.save_project_style('低饱和木刻版画', 0)
    elif kind == 'creative_agreement':
        current = ops.load_creative_agreement()
        ops.save_creative_agreement(current['project_defaults'] | {'tone': '克制'},
                                    current['project_revision'], '改创作约定', screenplay_id=current['screenplay_id'],
                                    expected_script_revision=current['script_revision'])
    else:
        ops.save_asset({'id': asset['id'], 'detail': '长发'})


@pytest.mark.parametrize('kind, key', [
    ('blocks', 'blocks'),
    ('project_style', 'project_style'),
    ('assets', 'assets'),
])
def test_write_rejects_during_generation_and_names_the_changed_key(project, monkeypatch, tmp_path, kind, key):
    ops, unit, block, shot, board = shot_with_frame(project, monkeypatch, tmp_path)
    asset = ops.save_asset({'category': 'appearance', 'title': '形象甲'})
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'create_object', 'name': '甲', 'kind': 'person', 'asset_id': asset['id']}])
    stub(monkeypatch, lambda: mutate(ops, unit, block, kind, asset))
    with pytest.raises(ValueError) as error:
        ops.generate_storyboard_sketch(unit, shot, 0, {}, board['revision'])
    message = str(error.value)
    assert '依据已变化' in message and key in message
    # 被拒的生成不写入任何东西：旧帧与原依据哈希都还在。
    current = ops.read_storyboard(unit)
    assert current['revision'] == board['revision']
    assert current['shots'][0]['keyframes'] == board['shots'][0]['keyframes']


def test_diagram_change_is_a_basis_difference(project, monkeypatch, tmp_path):
    ops, unit, block, shot, board = shot_with_frame(project, monkeypatch, tmp_path)
    saved = board['shots'][0]['keyframes'][0]['sketch_path']
    stub(monkeypatch, lambda: Image.new('RGB', (20, 12), 'black').save(saved))
    with pytest.raises(ValueError) as error:
        ops.generate_storyboard_sketch(unit, shot, 0, {}, board['revision'])
    assert 'frame_diagram' in str(error.value)


def test_handoff_frame_is_part_of_the_basis(project, monkeypatch, tmp_path):
    ops, unit, block = setup_board(project)
    first_image = tmp_path / 'first.png'
    Image.new('RGB', (20, 12), 'gray').save(first_image)
    created = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '递伞', 'duration_ms': 2000, 'source_block_ids': [block]},
        {'op': 'create_shot', 'title': '接伞的近景', 'duration_ms': 2000, 'source_block_ids': [block]}])
    first, second = created['shots'][0]['id'], created['shots'][1]['id']
    framed = ops.apply_storyboard_changes(unit, created['revision'], [
        {'op': 'set_keyframe', 'shot_id': first, 'offset_ms': 0, 'sketch_path': str(first_image)},
        {'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': first, 'ref_frame': 'last'}])
    stub(monkeypatch)
    board = ops.generate_storyboard_sketch(unit, second, 0, {}, framed['revision'])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    source = next(shot for shot in ops.read_storyboard(unit)['shots'] if shot['id'] == first)
    before = ops._storyboard_sketch_basis(ops.read_storyboard(unit),
                                          next(shot for shot in ops.read_storyboard(unit)['shots'] if shot['id'] == second),
                                          0, ops.read_script_unit_context(unit))[0]
    assert before['handoff']['frame_id'] == source['keyframes'][-1]['id']
    # 上一镜换了末帧：衔接帧依据随之变化，报错点名 handoff。
    replacement = tmp_path / 'replacement.png'
    Image.new('RGB', (20, 12), 'white').save(replacement)
    ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'set_keyframe', 'shot_id': first, 'offset_ms': 0, 'sketch_path': str(replacement)}])
    after = ops._storyboard_sketch_basis(ops.read_storyboard(unit),
                                         next(shot for shot in ops.read_storyboard(unit)['shots'] if shot['id'] == second),
                                         0, ops.read_script_unit_context(unit))[0]
    assert changed_basis_keys(before, after) == ['handoff']
    assert 'sketch_basis_changed' in codes(ops, unit)
