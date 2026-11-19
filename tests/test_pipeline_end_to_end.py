"""端到端验收：一句话建场，走到缺口与状态回报，整条链路的数据合同都对得上。

链路（按顺序真实调用，业务层不 mock）：

    建工程与一场正文 → 声明资产需求（场级/镜级） → 写镜头表与镜内时间轴
    → 绑定预演机位 → 抓框图 → 组装任务包 → 用假 provider 生成素描 → 绑定回关键帧
    → 依据稳定性检查 → 风格图 → 视频（首尾帧） → 缺口与状态回报

四条用例各守一件事：

1. `test_pipeline_runs_from_scene_to_gap_report`：整条链走得通，而且各环节交出的数据彼此对得上
   ——预演投影的框图哈希 = `read_storyboard_layout` 的哈希 = 任务包里的哈希 =
   落在关键帧 `sketch_source.layout.hash` 上的哈希；视频提交时算的依据在事后复核仍是「当前」。
2. `test_staging_drift_keeps_the_frame_but_a_diagram_change_marks_it`：出图依据只收稳定输入，
   改二维调度（对象位置/动作关键帧/命名时刻/声音节拍）不算依据变化，改框图才算，
   并且点名受影响的那一帧。
3. `test_gap_chain_separates_unit_and_shot_needs`：需求与引用分开存，`counts` 正确，
   镜头表视角的缺口数只算本镜（场级走 unit_gaps）。
4. `test_provider_failure_keeps_the_old_frame_and_receipt`：provider 抛错不绑定、不新增回执，
   旧图与旧收据一个字都不变。

假 provider 只 monkeypatch `storyboard_style_provider.render_sketch_image` /
`render_style_image` 与 `storyboard_video_provider.submit`（不联网、不碰真实 ComfyUI 与视频服务）；
技能文件、provider 库与预演根一律指到 tmp_path，不读仓库里的 `.runtime/` 样例，
也不碰 `~/.narrative/skills.json`。
"""
import copy
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative.generation_task import TaskProviderError
from narrative.skills import Skill, save_skills

pytestmark = pytest.mark.slow

CAMERA_ID = 'cam-a'
SKETCH_MOMENT = '起手'


# ------------------------------------------------------------------ 夹具与工具
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """外部路径全部落到 tmp_path：技能文件、预演根各一份，测试之间互不串味。"""
    monkeypatch.setenv('NARRATIVE_SKILLS_FILE', str(tmp_path / 'skills.json'))
    monkeypatch.setenv('NARRATIVE_PREVIS_PROJECT_ROOT', str(tmp_path / 'previs'))
    return tmp_path


def skills_file():
    return os.environ['NARRATIVE_SKILLS_FILE']


def skill(name, kind, *, model, stamp='2026-01-01T00:00:00Z'):
    """一条启用的生成技能：workflow.kind 决定它能干哪一类活，model 记的是哪个服务/模型。"""
    return Skill(name=name, summary='生成一次', instructions='按任务包生成',
                 created_at=stamp, updated_at=stamp,
                 workflow={'kind': kind, 'model': model})


def fake_sketch(monkeypatch, *, fail=None, calls=None):
    """假素描 provider：和真 provider 一样只写一张图并回执，不联网。"""
    def render(guide, prompt, copies_to, reference_images=()):
        if calls is not None:
            calls.append({'guide_exists': Path(guide).is_file(), 'prompt': prompt,
                          'references': [str(path) for path in reference_images]})
        if fail:
            raise ValueError(fail)
        Image.new('RGB', (480, 270), 'gray').save(copies_to)
        return {'provider': 'fake-comfy', 'job_id': 'job-e2e', 'output_sha256': 'e' * 8}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)


def fake_style(monkeypatch, *, calls=None):
    """假风格 provider：转图必须拿到一张已绑定的草图。

    出图按真实尺寸写（768×432）：视频服务对参考图有「每边至少 256 像素」的硬要求，
    尺寸太小的假图会让后面那一步在服务适配层就报错，测不到视频链路。
    """
    def render(sketch, prompt, copies_to, reference_images=()):
        assert Path(sketch).is_file()
        if calls is not None:
            calls.append({'sketch': str(sketch), 'prompt': prompt})
        Image.new('RGB', (768, 432), 'gray').save(copies_to)
        return {'provider': 'fake-comfy', 'job_id': 'job-style'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_style_image', render)


def fake_video(monkeypatch, box=None):
    """假视频服务：只把请求抄进 box 并回一个已排队的任务号。"""
    def submit(base_url, api_key, request):
        if box is not None:
            box.update({'base_url': base_url, 'api_key': api_key, 'request': request})
        return {'task_id': 'task-e2e', 'task': {'id': 'task-e2e', 'status': 'queued'}}
    monkeypatch.setattr('narrative.storyboard_video_provider.submit', submit)


def still_image(directory, name='frame.png'):
    """一张普通静帧：定案要求每镜都有起始草图，后几镜用它占位。"""
    path = directory / name
    Image.new('RGB', (16, 9), 'gray').save(path)
    return str(path)


def manual_box(**changes):
    """一帧手工框图：camera/objects/unmet_needs 必须整份给出（set_keyframe_layout 的合同）。"""
    return copy.deepcopy({
        'camera': {'x': 0.4, 'y': 1.6, 'z': 0.2, 'yaw': 12, 'pitch': -4, 'lens_mm': 35, 'framing': 'medium'},
        'objects': [{'asset_id': 'A1', 'bbox': [0.12, 0.24, 0.4, 0.92], 'depth': 2.4, 'kind': 'person'}],
        'unmet_needs': [], 'source': 'manual'}) | changes


def director_project(*, name='窗边三镜', duration=12):
    """一份最小的 DirectorDesk 预演工程：一台机位 + 一个演员，直接在测试里构造。

    只写 `.director` 的公开结构（format/entities/cuts），不依赖仓库里的任何样例文件。
    """
    return {
        'format': 'director-desk', 'version': 1, 'name': name,
        'duration': duration, 'fps': 24, 'aspect': '16:9',
        'room': {'enabled': False, 'width': 8, 'depth': 6, 'height': 2.8},
        'entities': [
            {'id': 'act-a', 'kind': 'actor', 'asset': 'person', 'name': '甲',
             'position': [0.0, 0.0, 0.0], 'rotation': [0, 0, 0], 'scale': [1, 1, 1],
             'visible': True, 'height': 1.75, 'path': None, 'clips': [], 'pose': {},
             'poseKeys': [], 'camera': None},
            {'id': CAMERA_ID, 'kind': 'camera', 'asset': 'camera', 'name': 'A · 主机位',
             'position': [0.0, 0.875, 5.0], 'rotation': [0, 0, 0], 'scale': [1, 1, 1],
             'visible': True, 'height': 1.75, 'path': None,
             'camera': {'aim': 'target', 'focal': 35, 'target': [0.0, 0.875, 0.0],
                        'targetId': '', 'targetHeight': 1.2, 'mode': 'free',
                        'offset': [0, 1.6, -1.8], 'inheritRotation': True, 'hideWalls': []}},
        ],
        'cuts': [{'time': 0, 'cameraId': CAMERA_ID}],
        'references': [],
    }


def write_previs(workspace, unit, payload=None, name='scene.director'):
    """预演工程写进 NARRATIVE_PREVIS_PROJECT_ROOT/previs/<场次编号>/：一个场景一个文件。"""
    directory = workspace / 'previs' / str(unit)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload or director_project(), ensure_ascii=False), encoding='utf-8')
    return path


def codes(ops, unit):
    return {row['code'] for row in ops.check_storyboard(unit)['diagnostics']}


def three_shots(ops, unit, block, durations=(3000, 1500, 500)):
    """一场三镜：标题与时长都写死，后面每条断言都按编号点名，不猜顺序。"""
    board = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': title, 'duration_ms': duration, 'source_block_ids': [block]}
        for title, duration in zip(('窗边等待', '反打', '空景'), durations, strict=True)])
    return board, [shot['id'] for shot in board['shots']]


def frame_at(board, shot_id, offset_ms=0):
    shot = next(row for row in board['shots'] if row['id'] == shot_id)
    return next(row for row in shot['keyframes'] if row['offset_ms'] == offset_ms)


# ------------------------------------------------------------------ 1 主链路
def test_pipeline_runs_from_scene_to_gap_report(workspace, project, monkeypatch):
    """这条用例守住「整条链能走通」：每一环的产物都是下一环的输入，且哈希沿路一致。"""
    # 0) 环境隔离：三条外部路径都在 tmp_path 下，仓库与用户目录一点没碰。
    assert Path(os.environ['NARRATIVE_PROVIDER_DB']).parent == workspace
    assert Path(os.environ['NARRATIVE_SKILLS_FILE']).parent == workspace
    assert Path(os.environ['NARRATIVE_PREVIS_PROJECT_ROOT']) == workspace / 'previs'

    ops, unit, block = setup_board(project)

    # 1) 建一场三镜：第 1 镜承载时间轴、预演与生成，后两镜验跨镜衔接与「只算本镜」。
    board, (first, second, third) = three_shots(ops, unit, block)
    assert [shot['title'] for shot in board['shots']] == ['窗边等待', '反打', '空景']
    assert [shot['duration_ms'] for shot in board['shots']] == [3000, 1500, 500]

    # 2) 声明资产需求：场级一条 + 第 3 镜镜级一条，都还没有引用落实。
    declared = ops.declare_asset_needs(
        unit, board['revision'],
        unit_needs=[{'kind': 'prop', 'name': '伞', 'variant': '折叠'}],
        shot_needs=[{'shot_id': third, 'needs': [{'kind': 'location', 'name': '街角'}]}])
    assert declared['revision'] == board['revision'] + 1
    assert declared['asset_needs'] == [{'kind': 'prop', 'name': '伞', 'variant': '折叠'}]
    assert next(row for row in declared['shots'] if row['id'] == third)['asset_needs'] == [
        {'kind': 'location', 'name': '街角'}]
    assert next(row for row in declared['shots'] if row['id'] == first)['asset_needs'] == []
    # 写路径与读路径给出同一份缺口计数：需求只描述「要什么」，不建卡、不绑引用。
    gap_counts = {'unit': 1, 'shot': 1, 'total': 2, 'resolved': 0, 'unresolved': 2, 'dangling': 0}
    assert declared['asset_needs_gap']['counts'] == gap_counts
    assert ops.read_asset_needs(unit)['counts'] == gap_counts

    # 3) 第 1 镜镜内时间轴：三个关键帧计划 + 两段；第 2 镜接第 1 镜末帧。
    planned = ops.apply_storyboard_changes(unit, declared['revision'], [
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 0,
         'moment_name': SKETCH_MOMENT, 'description': '甲抬头'},
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 1200, 'moment_name': '递伞'},
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 3000},
        {'op': 'set_timeline_segments', 'shot_id': first, 'segments': [[0, 1200], [1200, 3000]]},
        {'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': first, 'ref_frame': 'last'}])
    timeline = next(row for row in planned['shots'] if row['id'] == first)['timeline']
    assert [(row['offset_ms'], row['moment_name']) for row in timeline['keyframes']] == [
        (0, SKETCH_MOMENT), (1200, '递伞'), (3000, '')]
    assert timeline['segments'] == [[0, 1200], [1200, 3000]]
    assert timeline['duration_ms'] == 3000
    assert next(row for row in planned['shots'] if row['id'] == second)['handoff'] == {
        'from_shot': first, 'ref_frame': 'last'}

    # 4) 绑预演机位：自造的小 .director 放进 tmp_path 下的预演根，读摘要后绑机位。
    directory = write_previs(workspace, unit).parent
    summary = ops.read_previs(unit)
    assert summary['directory'] == str(directory)  # 环境变量生效，没去读仓库里的样例
    assert (summary['name'], summary['duration_s'], summary['camera_count'],
            summary['entity_count']) == ('窗边三镜', 12.0, 1, 1)
    assert summary['cameras'] == [{'camera_id': CAMERA_ID, 'name': 'A · 主机位', 'focal': 35.0,
                                   'position': [0.0, 0.875, 5.0]}]
    assert summary['bindings'] == []
    bound = ops.set_shot_previs(unit, planned['revision'], first, CAMERA_ID)
    assert bound['previs'] == {'scene_id': 'scene', 'camera_id': CAMERA_ID, 'time_offset_ms': 0}
    assert ops.read_previs(unit)['bindings'] == [
        {'shot_id': first, 'title': '窗边等待', 'previs': bound['previs']}]

    # 5) 抓框图：投影落在第 1 镜 0ms，只读工具必须读到同一份框图与哈希。
    captured = ops.capture_previs_layout(unit, first, 0)
    assert (captured['camera_id'], captured['time_ms']) == (CAMERA_ID, 0)
    assert captured['layout']['source'] == 'previs'
    assert (captured['layout']['shot_id'], captured['layout']['offset_ms']) == (first, 0)
    assert captured['layout']['camera']['framing'] == captured['framing']
    assert captured['layout']['objects'] and all(
        row['asset_id'].startswith('previs:') for row in captured['layout']['objects'])
    layout_view = ops.read_storyboard_layout(unit, first)
    assert layout_view['keyframe_offsets'] == [0, 1200, 3000]
    assert layout_view['count'] == 1 and layout_view['layouts'][0]['offset_ms'] == 0
    assert layout_view['layouts'][0]['layout']['hash'] == captured['layout']['hash']

    # 6) 组装任务包并生成素描：技能按 workflow.kind 选中，任务包里的框图就是这一帧的框图。
    #    项目风格是出图依据的一部分，必须在生成之前定下来——生成完再改风格，这一帧就会被
    #    正当地判成「依据已变化」（第 2 条用例钉的就是这条口径）。
    ops.save_project_style('低饱和木刻版画', 0)
    save_skills([skill('草图·窗边', 'sketch', model='stub-sketch')], skills_file())
    calls = []
    fake_sketch(monkeypatch, calls=calls)
    sketch = ops.run_generation_task(unit, captured['revision'], 'sketch', first, 0,
                                     prompt='窗边的光更冷')
    assert sketch['bound_to'] == 'keyframe_sketch'
    assert sketch['storyboard_revision'] == captured['revision'] + 1
    # 回执里点名用的是哪条技能、哪个 provider：用户启用的技能压过内置默认，不能被标成内置。
    assert (sketch['skill']['name'], sketch['skill']['model']) == ('草图·窗边', 'stub-sketch')
    assert sketch['skill']['license'] == ''
    assert sketch['skill'].get('builtin', False) is False
    assert sketch['provider'] == {'provider': 'fake-comfy', 'job_id': 'job-e2e',
                                  'output_sha256': 'e' * 8}
    assert sketch['warnings'] == []
    assert sketch['task']['layout']['hash'] == captured['layout']['hash']
    assert sketch['task']['moment'] == {'name': SKETCH_MOMENT, 'description': '甲抬头'}
    assert sketch['task']['constraints']['duration_ms'] == 3000
    assert sketch['task']['constraints']['aspect'] == '16:9'
    assert sketch['task']['style']['text'] == '低饱和木刻版画'  # 项目风格随任务包带进生成
    # provider 拿到的是程序画的构图参考图（不是交付图），提示词带着命名时刻与本次补充要求。
    assert calls[0]['guide_exists'] is True
    assert SKETCH_MOMENT in calls[0]['prompt'] and '窗边的光更冷' in calls[0]['prompt']

    # 7) 绑定回关键帧：图落在该帧上，收据记着 skill 与 provider，框图哈希也随帧留档。
    current = ops.read_storyboard(unit)
    frame = frame_at(current, first)
    assert frame['sketch_path'] == sketch['output_path'] and Path(frame['sketch_path']).is_file()
    assert frame['sketch_source']['provider'] == 'fake-comfy'
    assert frame['sketch_source']['job_id'] == 'job-e2e'
    assert frame['sketch_source']['user_prompt'] == '窗边的光更冷'
    assert frame['sketch_source']['layout']['hash'] == captured['layout']['hash']
    assert frame['sketch_basis_hash']

    # 8) 定案 → 风格图 → 视频（首尾帧）：定案要求每镜都有起始草图，后两镜补普通静帧。
    image = still_image(workspace)
    framed = ops.apply_storyboard_changes(unit, sketch['storyboard_revision'], [
        {'op': 'set_keyframe', 'shot_id': second, 'offset_ms': 0, 'sketch_path': image},
        {'op': 'set_keyframe', 'shot_id': third, 'offset_ms': 0, 'sketch_path': image}])
    approved = ops.apply_storyboard_changes(
        unit, framed['revision'], [{'op': 'set_approved', 'approved': True}])
    assert approved['approved'] is True

    save_skills([skill('草图·窗边', 'sketch', model='stub-sketch'),
                 skill('风格帧', 'style', model='stub-style'),
                 skill('视频·首尾帧', 'video', model='MiniMax-H3')], skills_file())
    style_calls = []
    fake_style(monkeypatch, calls=style_calls)
    styled = ops.run_generation_task(unit, approved['revision'], 'style', first, 0)
    assert styled['bound_to'] == 'keyframe_style'
    assert styled['skill']['name'] == '风格帧' and Path(styled['output_path']).is_file()
    # 转图的输入就是第 7 步绑上的那张草图（数据合同：source_image 来自该帧）。
    assert Path(style_calls[0]['sketch']) == Path(frame['sketch_path'])
    assert Path(styled['task']['constraints']['source_image']) == Path(frame['sketch_path'])

    submitted = {}
    fake_video(monkeypatch, submitted)
    monkeypatch.setenv('MINIMAX_API_KEY', 'secret-e2e')
    # 视频服务的时长是「要生成多长」，不是镜长：5..15 整秒由服务决定（本镜 3000ms），
    # 必须显式给 constraints，程序不替用户把镜长当成生成时长。
    video = ops.run_generation_task(unit, styled['storyboard_revision'], 'video', first, 0,
                                    constraints={'duration_ms': 5000})
    assert video['bound_to'] == 'shot_generations' and video['output_path'] is None
    assert video['skill']['name'] == '视频·首尾帧'
    assert video['task']['constraints']['duration_ms'] == 5000
    assert submitted['request']['duration'] == 5
    assert [row.get('role') for row in submitted['request']['content'] if 'role' in row] == ['first_frame']
    assert video['provider']['task_id'] == 'task-e2e'
    generation = next(row for row in ops.read_storyboard(unit)['shots'] if row['id'] == first)[
        'generations'][-1]
    assert (generation['status'], generation['mode']) == ('submitted', 'endpoints')
    assert (generation['provider_task_id'], generation['duration_ms']) == ('task-e2e', 5000)
    assert generation['generated_by'] == {'skill': '视频·首尾帧', 'kind': 'video'}
    # 审阅过的请求文件从临时目录复制进了工程：事后复核读得到。
    assert Path(generation['request_path']).is_file() and generation['request_sha256']
    assert 'secret-e2e' not in json.dumps(video, ensure_ascii=False)  # 密钥只用于提交

    # 9) 缺口与状态回报：刚生成的帧不判依据变化，视频提交时的依据事后复核仍是「当前」。
    report = ops.check_storyboard(unit)
    report_codes = {row['code'] for row in report['diagnostics']}
    assert 'sketch_basis_changed' not in report_codes
    assert 'generation_basis_changed' not in report_codes
    assert report['affected_frames'] == []
    assert len(report['generations']) == 1
    reviewed = report['generations'][0]
    assert (reviewed['shot_id'], reviewed['generation_id'], reviewed['status']) == (
        first, generation['id'], 'submitted')
    assert (reviewed['basis_status'], reviewed['stale']) == ('current', False)
    gaps = ops.read_asset_needs(unit)
    assert gaps['counts'] == gap_counts
    assert [(row['scope'], row['shot_id'], row['name']) for row in gaps['needs']] == [
        ('unit', None, '伞'), ('shot', third, '街角')]
    assert all(row['bound_asset_id'] is None for row in gaps['needs'])


# ------------------------------------------------------------------ 2 依据纪律
def test_staging_drift_keeps_the_frame_but_a_diagram_change_marks_it(workspace, project, monkeypatch):
    """这条用例守住「出图依据只收人写下的稳定输入」。

    排演怎么变都不该判「依据已变化」（否则每调一次调度就要重画一遍）；框图变了才该提示重画。
    """
    ops, unit, block = setup_board(project)
    board = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '窗边等待', 'duration_ms': 3000, 'source_block_ids': [block]}])
    shot = board['shots'][0]['id']
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 0, 'moment_name': '递伞'},
        {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': manual_box()}])
    save_skills([skill('草图·窗边', 'sketch', model='stub-sketch')], skills_file())
    fake_sketch(monkeypatch)
    generated = ops.run_generation_task(unit, board['revision'], 'sketch', shot, 0)
    assert 'sketch_basis_changed' not in codes(ops, unit)
    before = ops.read_storyboard(unit)
    frame = frame_at(before, shot)

    # 1) 二维调度：对象位置、动作关键帧、命名时刻、声音节拍全都改一遍。
    staged = ops.apply_storyboard_changes(unit, generated['storyboard_revision'], [
        {'op': 'create_object', 'name': '甲', 'kind': 'person', 'x': .2, 'y': .8}])
    actor = staged['objects'][0]['id']
    staged = ops.apply_storyboard_changes(unit, staged['revision'], [
        {'op': 'update_object', 'object_id': actor, 'x': .7, 'y': .3, 'rotation': 45},
        {'op': 'set_action_keyframe', 'object_id': actor, 'time_ms': 0, 'x': .2,
         'interpolation': 'linear'},
        {'op': 'create_marker', 'name': '递伞时刻', 'time_ms': 900},
        {'op': 'update_timeline_keyframe', 'shot_id': shot, 'offset_ms': 0,
         'moment_name': '递伞到手'},
        {'op': 'create_sound', 'shot_id': shot, 'kind': 'silence', 'label': '雨停一拍',
         'time_ms': 600, 'duration_ms': 300}])
    sound = staged['shots'][0]['timeline']['sound_cues'][0]['sound_id']
    moved = ops.apply_storyboard_changes(unit, staged['revision'], [
        {'op': 'update_sound', 'shot_id': shot, 'sound_id': sound, 'time_ms': 900}])
    # 排演确实变了（位置、命名时刻、声音节拍都动了），但依据没变。
    assert moved['objects'][0]['x'] == .7
    assert moved['shots'][0]['timeline']['sound_cues'][0]['offset_ms'] == 900
    assert 'sketch_basis_changed' not in codes(ops, unit)
    assert ops.check_storyboard(unit)['affected_frames'] == []
    kept = frame_at(ops.read_storyboard(unit), shot)
    assert kept['sketch_path'] == frame['sketch_path']
    assert kept['sketch_basis_hash'] == frame['sketch_basis_hash']

    # 2) 改框图：已生成的帧被判「依据已变化」，warning 且点名这一帧，旧图原样保留。
    ops.apply_storyboard_changes(unit, moved['revision'], [
        {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
         'layout': manual_box(camera=manual_box()['camera'] | {'yaw': 40})}])
    report = ops.check_storyboard(unit)
    warning = next(row for row in report['diagnostics'] if row['code'] == 'sketch_basis_changed')
    assert warning['severity'] == 'warning'
    assert (warning['shot_id'], warning['frame_id']) == (shot, frame['id'])
    assert report['affected_frames'] == [{'shot_id': shot, 'frame_id': frame['id']}]
    # 依据变了只提示重画，不动旧图，也不改当时记下的依据哈希。
    marked = frame_at(ops.read_storyboard(unit), shot)
    assert marked['sketch_path'] == frame['sketch_path']
    assert marked['sketch_basis_hash'] == frame['sketch_basis_hash']


# ------------------------------------------------------------------ 3 缺口链路
def test_gap_chain_separates_unit_and_shot_needs(workspace, project):
    """这条用例守住「需求与引用分开存」：counts 正确，镜头表的缺口数只算本镜。

    声明两条镜级需求（一条已绑到资产卡、一条没有），外加一条未落实的场级需求：
    这样一来「场级不摊到每镜」「其它镜不承担本镜缺口」两件事都能被观测到。
    """
    ops, unit, block = setup_board(project)
    board, (first, second, _third) = three_shots(ops, unit, block)
    # 一张有参考图的人物卡：需求靠「引用」落实，卡得是 appearance 且已有图（否则算引用悬空）。
    card = ops.save_asset({'category': 'appearance', 'title': '形象乙',
                           'reference_images': [{'path': still_image(workspace, 'card.png')}]})
    ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'update_shot', 'shot_id': first,
         'visual_elements': [{'kind': 'person', 'name': '乙', 'usage': '出场', 'asset_id': card['id']}]}])
    declared = ops.declare_asset_needs(
        unit, ops.read_storyboard(unit)['revision'],
        unit_needs=[{'kind': 'prop', 'name': '伞'}],
        shot_needs=[{'shot_id': first, 'needs': [{'kind': 'person', 'name': '甲'},
                                                 {'kind': 'person', 'name': '乙'}]}])
    view = ops.read_asset_needs(unit)
    # 三条需求：场级 1、镜级 2；乙由引用落实，甲与伞没有。
    assert view['counts'] == {'unit': 1, 'shot': 2, 'total': 3, 'resolved': 1,
                              'unresolved': 2, 'dangling': 0}
    assert declared['asset_needs_gap']['counts'] == view['counts']
    assert [(row['scope'], row['shot_id'], row['name'], row['bound_asset_id'])
            for row in view['needs']] == [('unit', None, '伞', None),
                                          ('shot', first, '甲', None),
                                          ('shot', first, '乙', card['id'])]
    assert view['dangling'] == []
    # 资产分析上下文带的是同一份派生缺口视图，不是另一套算法。
    assert ops.read_storyboard_assets_context(unit, [first])['asset_needs']['counts'] == view['counts']

    # 镜头表视角（NativeBridge.shotTable 用的就是这两个 classmethod）：
    # 第 1 镜只算自己那条未落实的「甲」，第 2 镜一条都没有，场级的「伞」只出现在 unit_gaps。
    from narrative.native_bridge import NativeBridge

    board = ops.read_storyboard(unit)
    rows = {index: NativeBridge._shot_row(shot, index, NativeBridge._shot_gaps(
        view, shot['id'], shot=shot), []) for index, shot in enumerate(board['shots'], 1)}
    assert [rows[index]['gap_count'] for index in (1, 2)] == [1, 0]
    assert [(row['gap'], row['scope'], row['name']) for row
            in NativeBridge._shot_gaps(view, first, shot=board['shots'][0])] == [
        ('unresolved', 'shot', '甲')]
    assert NativeBridge._shot_gaps(view, second, shot=board['shots'][1]) == []
    assert [(row['gap'], row['scope'], row['name'])
            for row in NativeBridge._shot_gaps(view, '', scope='unit')] == [('unresolved', 'unit', '伞')]


# ------------------------------------------------------------------ 4 失败不污染
def test_provider_failure_keeps_the_old_frame_and_receipt(workspace, project, monkeypatch):
    """这条用例守住「失败不绑定」：服务失败是错误而不是内容问题，旧图与旧收据一个字不动。"""
    ops, unit, block = setup_board(project)
    board, (first, _second, _third) = three_shots(ops, unit, block)
    # 生成只认该镜时间轴上已有的时刻：先给它一个 0ms 的计划关键帧。
    board = ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 0, 'moment_name': '递伞'}])
    save_skills([skill('草图·窗边', 'sketch', model='stub-sketch')], skills_file())
    fake_sketch(monkeypatch)
    first_run = ops.run_generation_task(unit, board['revision'], 'sketch', first, 0)
    before = ops.read_storyboard(unit)
    assert Path(frame_at(before, first)['sketch_path']).is_file()

    fake_sketch(monkeypatch, fail='ComfyUI 服务未启动')
    with pytest.raises(TaskProviderError, match='生成服务调用失败') as error:
        ops.run_generation_task(unit, first_run['storyboard_revision'], 'sketch', first, 0,
                                prompt='重画一版')
    assert 'ComfyUI 服务未启动' in str(error.value)

    # 整块分镜（含修订号）与失败前一模一样：没有新帧、没有新回执、没有半张图。
    after = ops.read_storyboard(unit)
    assert after == before
    frame = frame_at(after, first)
    assert frame['sketch_path'] == first_run['output_path']
    assert Path(frame['sketch_path']).is_file()
    assert frame['sketch_source'] == frame_at(before, first)['sketch_source']
    assert frame['sketch_source']['job_id'] == 'job-e2e'
    assert frame['sketch_basis_hash'] == frame_at(before, first)['sketch_basis_hash']
    assert 'generations' not in next(row for row in after['shots'] if row['id'] == first)
    report = ops.check_storyboard(unit)
    assert report['affected_frames'] == []
    assert 'sketch_basis_changed' not in {row['code'] for row in report['diagnostics']}
