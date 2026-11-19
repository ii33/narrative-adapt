"""预演灯光 → 生图提示词：抽取、描述、插进提示词，以及两处接线的护栏。

每条断言都能手算：场景是手工构造的小房间（机位在 +z 看 -z、演员站在原点），
灯位、色值、聚光角与强度都是整数，所以「朝右下」「硬光」「偏暖」「高光比」
这些结论来自阈值本身，不是来自跑出来的文本。
"""
import zipfile

import pytest
from PIL import Image
from test_screenplay_source_references import prepared as prepared
from test_storyboard_assets import board as board

from narrative.lighting import light_description, lights_from_scene, merge_into_prompt
from narrative.previs_scene import camera_at

# 手工构造的场景与灯位 ---------------------------------------------------------

def camera():
    """一台机位：位置 (0, 1.6, 5)，注视点 (0, 1.6, 0)——视线正朝 -z。"""
    return {'id': 'cam-1', 'name': 'A · 主机位', 'kind': 'camera', 'position': [0.0, 1.6, 5.0],
            'rotation': [0.0, 0.0, 0.0], 'path': [], 'focal': 35.0, 'focal_keys': [],
            'target': [0.0, 1.6, 0.0], 'target_id': None, 'target_height': 0.0,
            'target_path': [], 'aim': 'target', 'mode': 'free'}


def actor():
    """演员站在原点、身高 1.75 m：主体中心因此是 (0, 0.875, 0)。"""
    return {'id': 'act-1', 'name': '甲', 'kind': 'actor', 'position': [0.0, 0.0, 0.0],
            'rotation': [0.0, 0.0, 0.0], 'scale': [1.0, 1.0, 1.0], 'height': 1.75, 'path': []}


def light(**fields):
    """一盏灯（字段与 previs_scene.load_scene 的 lights 行同口径）。"""
    row = {'id': 'light-1', 'name': '主光', 'kind': 'light', 'asset': 'light-spot',
           'position': [-1.5, 3.0, 2.5], 'rotation': [0.0, 0.0, 0.0], 'color': '#ffd9a8',
           'intensity': 100.0, 'temperature': None, 'angle': 25.0, 'range': 12.0,
           'penumbra': 0.3, 'width': 3.0, 'height': 2.0}
    row.update(fields)
    return row


def scene(lights, lighting=None, entities=None, raw=None):
    return {'path': None, 'scene_id': 'scene-1', 'name': '灯位场', 'duration_s': 8.0, 'fps': 24,
            'aspect': '16:9', 'aspect_ratio': 16 / 9, 'cameras': [camera()],
            'entities': [actor()] if entities is None else entities,
            'cuts': [{'time': 0.0, 'camera_id': 'cam-1'}],
            'lights': list(lights), 'lighting': lighting, 'scenes': [], 'raw': raw}


def three_lights():
    """三点布光：侧前偏暖硬光做主光、右前方一盏补光、身后一盏轮廓光。

    归一化亮度（点/聚光参考值 100）：主光 100→1.0，补光 12→0.12，轮廓光 8→0.08，
    所以主光是 1.0，其余之和 0.20，光比 5.0 ≥ 4 → 高光比。
    """
    return [light(),
            light(id='light-2', name='补光', asset='light-point', position=[2.5, 1.8, 1.2],
                  color='#d8e2f0', intensity=12.0, angle=None, range=9.0),
            light(id='light-3', name='轮廓光', position=[0.3, 2.6, -3.0], color='#ffffff',
                  intensity=8.0, angle=35.0, range=6.0)]


def subjects(subject=None):
    """主体位置：与场景里的演员同一个点（脚底 + 半身高）。"""
    return [subject or {'position': [0.0, 0.0, 0.0], 'height': 1.75}]


def frame(scene_value=None, time_s=0.0):
    return camera_at(scene_value or scene(three_lights()), time_s, 'cam-1')


# 抽取 -------------------------------------------------------------------------

def test_lights_are_read_out_with_the_units_the_project_uses():
    lights = lights_from_scene(scene(three_lights(), {'ambient': 2.5, 'exposure': 1.05}))
    body = [row for row in lights if row['kind'] != 'ambient']
    assert len(body) == 3
    for row in body:
        # 对外约定的七个键都在（intensity 是 .director 的原始强度，distance 是衰减距离）
        assert {'kind', 'color', 'intensity', 'position', 'target', 'angle', 'distance'} <= set(row)
    key = next(row for row in body if row['kind'] == 'key')
    assert key['id'] == 'light-1' and key['intensity'] == 100.0 and key['angle'] == 25.0
    # 角色粗判：身后那盏是轮廓光，最亮的是主光，剩下的是补光
    roles = {row['id']: row['kind'] for row in body}
    assert roles == {'light-1': 'key', 'light-2': 'fill', 'light-3': 'rim'}
    # 环境光是另一条数据：没有位置，但有亮度
    ambient = next(row for row in lights if row['kind'] == 'ambient')
    assert ambient['position'] is None and ambient['intensity'] == 2.5
    # 光照点：灯 entity 的 rotation 是 0 时朝 -z（DirectorDesk 把 target 放在局部 (0,0,-1)）
    assert key['target'] == pytest.approx([-1.5, 3.0, 2.5 - 1.0])


def test_page_snapshot_lights_carry_position_but_no_photometric_values():
    """预演页 getScene 不带灯的强度/色温/聚光角：这时只能读出方向，不许编参数。"""
    raw = {'entities': [{'id': 'light-9', 'kind': 'prop', 'asset': 'light-spot',
                         'position': [-1.5, 3.0, 2.5], 'rotation': [0.0, 0.0, 0.0]}]}
    scene_value = scene([], entities=[], raw=raw)
    lights = lights_from_scene(scene_value)
    assert len(lights) == 1
    assert lights[0]['intensity'] is None and lights[0]['color'] == ''
    assert lights[0]['angle'] is None and lights[0]['distance'] is None
    # 机位在场景里，所以方位仍说得出来；硬软/色温/光比一律不提
    assert light_description(scene_value, frame(), subjects()) == '主光自侧前方，投影朝右上。'


# 描述 -------------------------------------------------------------------------

def test_three_point_light_reads_as_a_side_front_warm_hard_key():
    scene_value = scene(three_lights(), {'ambient': 2.5})
    sketch = light_description(scene_value, frame(), subjects(), detail='sketch')
    style = light_description(scene_value, frame(), subjects(), detail='style')
    # 素描版：方向（30.96°→侧前方）、硬软（聚光角 25°→硬光）、影子（灯在左前、比主体更靠机位一侧→右上）
    assert sketch == '主光自侧前方，硬光，投影朝右上。'
    # 风格版：再补色温（#ffd9a8→偏暖）、光比（1.0/(0.12+0.08)=5→高光比）与暗部占比
    assert style == '主光自侧前方，硬光、偏暖，高光比（暗部约四成），投影朝右上。'


def test_no_lights_and_ambient_only_do_not_invent_a_light():
    assert light_description(scene([]), frame(), subjects()) == ''
    assert light_description(scene([], {'ambient': 2.5}), frame(), subjects()) == '均匀环境光'
    # 预演里的灯被排除（这里用空 list 模拟）：也不会退回「均匀环境光」凭空补一句
    assert light_description(scene([]), frame(), subjects(), detail='style') == ''


def test_sketch_stays_terse_while_style_carries_colour_and_ratio():
    scene_value = scene(three_lights(), {'ambient': 2.5})
    sketch = light_description(scene_value, frame(), subjects(), detail='sketch')
    style = light_description(scene_value, frame(), subjects(), detail='style')
    assert len(sketch) <= 40 and len(style) <= 90
    # 素描不碰色温与光比：这两样属于风格帧的口径
    assert '偏暖' not in sketch and '光比' not in sketch
    assert '偏暖' in style and '光比' in style and '暗部' in style
    assert len(style) > len(sketch)
    with pytest.raises(ValueError):
        light_description(scene_value, frame(), subjects(), detail='video')


def test_shadow_side_flips_when_the_camera_moves_to_the_other_side():
    """同一场同一盏灯：机位换到对面，光位没动，影子就落到另一边（光影跟着机位翻）。"""
    scene_value = scene([light()], {'ambient': 2.5})
    assert light_description(scene_value, frame(), subjects()) == '主光自侧前方，硬光，投影朝右上。'
    mirrored = camera() | {'position': [0.0, 1.6, -5.0], 'target': [0.0, 1.6, 0.0]}
    opposite = camera_at(scene_value | {'cameras': [mirrored]}, 0.0, 'cam-1')
    assert light_description(scene_value, opposite, subjects()) == '主光自侧后方，硬光，投影朝左下。'


def test_a_light_deeper_than_the_subject_throws_its_shadow_towards_the_camera():
    """灯比主体更靠画面深处（背后的窗）：影子朝机位倒，画面上落在右下——也就是最常见的画法。"""
    behind = light(name='窗光', position=[-1.5, 3.0, -3.5], angle=60.0)
    scene_value = scene([behind], {'ambient': 2.5})
    # 水平方位 156.8°（背面）；聚光角 60° ≥ 45° → 柔光；影子朝右下
    assert light_description(scene_value, frame(), subjects()) == '主光自背面，柔光，投影朝右下。'


# 插进提示词 -------------------------------------------------------------------

def test_merge_puts_the_light_line_right_after_the_drawing_method():
    base = ('统一草图呈现规范：\n画法：用少量轮廓线归纳形体。\n明暗：光照要在主体上产生结果。')
    merged = merge_into_prompt(base, '主光自侧前方，硬光，投影朝右下。')
    assert merged.split('\n') == ['统一草图呈现规范：', '画法：用少量轮廓线归纳形体。',
                                  '光线：主光自侧前方，硬光，投影朝右下。',
                                  '明暗：光照要在主体上产生结果。']


def test_merge_appends_only_when_the_prompt_has_no_section_to_hang_on():
    base = '镜头：等候\n项目风格：低饱和木刻版画'
    merged = merge_into_prompt(base, '主光自侧前方，硬光。')
    assert merged == base + '\n光线：主光自侧前方，硬光。'
    # 结尾已经有换行时不再多加一行空行
    assert merge_into_prompt(base + '\n', '主光自侧前方，硬光。') == base + '\n光线：主光自侧前方，硬光。'


def test_merge_keeps_an_existing_light_paragraph_and_adds_a_parallel_line():
    base = '镜头：等候\n光线：午后侧光，窗影落在桌面。\n构图：人物偏左。'
    merged = merge_into_prompt(base, '主光自侧前方，硬光、偏暖。')
    # 人写的光线段落一个字都没动，新的一句紧跟在它后面（并列，不覆盖）
    assert '光线：午后侧光，窗影落在桌面。' in merged
    assert merged.split('\n') == ['镜头：等候', '光线：午后侧光，窗影落在桌面。',
                                  '光线：主光自侧前方，硬光、偏暖。', '构图：人物偏左。']


def test_merge_is_a_no_op_without_a_description_and_never_duplicates_the_line():
    base = '画法：用少量轮廓线。\n明暗：按光组织。'
    assert merge_into_prompt(base, '') == base
    once = merge_into_prompt(base, '主光自侧面。')
    assert merge_into_prompt(once, '主光自侧面。') == once


# 接线：故事板提示词 -----------------------------------------------------------

def sketch_provider(monkeypatch, seen):
    """替掉图像服务：收下提示词、回一张假图（接线测的是提示词，不是生成）。"""
    def render(guide, prompt, copies_to, reference_images=()):
        seen.append(prompt)
        Image.new('RGB', (64, 36), 'white').save(copies_to)
        return {'provider': 'stub-only', 'job_id': 'lighting-test'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)


@pytest.mark.slow
def test_sketch_prompt_gains_exactly_one_light_line_when_a_scene_is_available(board, monkeypatch):
    ops, _script, unit, b = board
    seen = []
    sketch_provider(monkeypatch, seen)
    shot = b['shots'][0]['id']
    # 没有预演（也没绑定）：照旧出图，提示词里一个光线字都没有——这是「不影响现有行为」的护栏
    first = ops.generate_storyboard_sketch(unit, shot, 0, {}, b['revision'])
    without = seen[-1]
    assert '光线：' not in without
    assert not (ops.read_storyboard(unit)['shots'][0].get('previs'))
    # 给一份假场景：同一次生成的提示词只多出这一行，别的一个字都不变
    fake = scene(three_lights(), {'ambient': 2.5})
    monkeypatch.setattr('narrative.storyboard_art.shot_light_scene', lambda *args, **kwargs: fake)
    ops.generate_storyboard_sketch(unit, shot, 0, {}, first['revision'])
    with_light = seen[-1]
    line = '光线：' + light_description(fake, frame(fake), subjects())
    assert line == '光线：主光自侧前方，硬光，投影朝右上。'
    assert line in with_light
    assert with_light.replace(line + '\n', '') == without


@pytest.mark.slow
def test_style_package_prompt_adds_the_style_light_line_beside_the_project_style(board, monkeypatch, tmp_path):
    ops, _script, unit, b = board
    seen = []
    sketch_provider(monkeypatch, seen)
    shot = b['shots'][0]['id']
    # 定案要求每个镜头都有起始草图：先给所有镜头各出一张（真图由桩件代替）
    current = b
    for row in b['shots']:
        current = ops.generate_storyboard_sketch(unit, row['id'], 0, {}, current['revision'])
    # 项目风格里人写了一段光线：接线只能并列，不许覆盖
    ops.save_project_style('低饱和木刻版画\n光线：午后侧光，窗影落在桌面。', 0)
    ops.apply_storyboard_changes(unit, current['revision'], [{'op': 'set_approved', 'approved': True}])

    without = tmp_path / '无预演.zip'
    ops.export_storyboard_style_package(unit, shot, str(without))
    plain = _prompt_of(without)
    assert '光线：午后侧光，窗影落在桌面。' in plain and '主光自' not in plain

    fake = scene(three_lights(), {'ambient': 2.5})
    monkeypatch.setattr('narrative.storyboard_art.shot_light_scene', lambda *args, **kwargs: fake)
    with_light_path = tmp_path / '有预演.zip'
    ops.export_storyboard_style_package(unit, shot, str(with_light_path))
    merged = _prompt_of(with_light_path)
    line = '光线：' + light_description(fake, frame(fake), subjects(), detail='style')
    assert line == '光线：主光自侧前方，硬光、偏暖，高光比（暗部约四成），投影朝右上。'
    assert line in merged
    # 人写的那一段还在，且新的一句紧跟在它后面（并列）
    assert merged.index(line) == merged.index('光线：午后侧光，窗影落在桌面。') + len('光线：午后侧光，窗影落在桌面。') + 1
    # 去掉这一行就是没有预演时的原文：除它之外一个字都没变
    assert merged.replace(line + '\n', '') == plain


def _prompt_of(package):
    with zipfile.ZipFile(package) as archive:
        return archive.read('prompt.txt').decode('utf-8')


def test_shot_light_scene_prefers_the_scene_the_caller_passes_in():
    """调用方直接给场景时不碰预演页、不读文件：给什么就用什么。"""
    from narrative.storyboard_art import shot_light_scene

    fake = scene(three_lights(), {'ambient': 2.5})
    assert shot_light_scene(None, {}, 'unit-1', fake) is fake


def test_shot_light_scene_is_none_without_a_previs_binding():
    """没绑定预演机位就是没有场景（不报错、不猜），提示词拼装照旧。"""
    from narrative.storyboard_art import shot_light_scene

    assert shot_light_scene(None, {'id': 'shot-1'}, 'unit-1') is None
    assert shot_light_scene(None, {'previs': {'camera_id': ''}}, 'unit-1') is None
