"""B4 生成任务包与执行器：组装、技能选择、执行、绑定与失败语义。

用假的 provider（monkeypatch render_sketch_image / render_style_image / submit）跑，
不联网、不碰真实 ComfyUI 与视频服务；技能文件一律指到 tmp_path，
绝不碰 ~/.narrative/skills.json。
"""
import json
import os
from pathlib import Path

import pytest
from PIL import Image
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative import generation_task, tool_api
from narrative.generation_task import (
    TaskInputError,
    TaskProviderError,
    build_task,
    execute,
    select_skill,
    task_prompt,
)
from narrative.skills import Skill, save_skills

pytestmark = pytest.mark.slow

BOX = {'camera': {'x': 0.4, 'y': 1.6, 'z': 0, 'yaw': 10, 'pitch': -3, 'lens_mm': 40, 'framing': 'medium'},
       'objects': [{'asset_id': 'asset-1', 'bbox': [0.1, 0.2, 0.4, 0.9], 'depth': 2.5, 'kind': 'person'}],
       'unmet_needs': [], 'source': 'manual'}


@pytest.fixture(autouse=True)
def skills_file(tmp_path, monkeypatch):
    path = tmp_path / 'skills.json'
    monkeypatch.setenv('NARRATIVE_SKILLS_FILE', str(path))
    return path


def skill(name, kind, *, model='stub-model', enabled=True, stamp='2026-01-01T00:00:00Z', **workflow):
    return Skill(name=name, summary='生成一次', instructions='按任务包生成',
                 enabled=enabled, created_at=stamp, updated_at=stamp,
                 workflow={'kind': kind, 'model': model, **workflow})


def write(path, *rows):
    save_skills(list(rows), path)
    return path


@pytest.fixture
def scene(project, tmp_path):
    """一个镜头、一段正文依据、offset 0 已绑定草图。"""
    ops, unit, block = setup_board(project)
    image = tmp_path / 'frame.png'
    Image.new('RGB', (16, 9), 'gray').save(image)
    created = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '窗边等待', 'duration_ms': 3000, 'source_block_ids': [block]}])
    shot = created['shots'][0]['id']
    framed = ops.apply_storyboard_changes(unit, created['revision'], [
        {'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 0, 'sketch_path': str(image)}])
    return ops, unit, shot, framed


def fake_sketch(monkeypatch, *, fail=None, calls=None):
    def render(guide, prompt, copies_to, reference_images=()):
        if calls is not None:
            calls.append({'guide_exists': Path(guide).is_file(), 'prompt': prompt,
                          'references': [str(path) for path in reference_images],
                          'workflow': os.environ.get('NARRATIVE_COMFY_SKETCH_WORKFLOW')})
        if fail:
            raise ValueError(fail)
        Image.new('RGB', (24, 14), 'gray').save(copies_to)
        return {'provider': 'fake-comfy', 'job_id': 'job-9', 'output_sha256': 'x' * 8}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)


def fake_style(monkeypatch, *, fail=None):
    def render(sketch, prompt, copies_to, reference_images=()):
        assert Path(sketch).is_file()
        if fail:
            raise ValueError(fail)
        Image.new('RGB', (24, 14), 'gray').save(copies_to)
        return {'provider': 'fake-comfy', 'job_id': 'job-style'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_style_image', render)


# ------------------------------------------------------------------ 任务包
def test_build_task_shape_and_defaults(scene):
    ops, unit, shot, board = scene
    ops.apply_storyboard_changes(unit, board['revision'], [
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 0, 'moment_name': '递伞', 'description': '手指接触'},
        {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': BOX}])
    current = ops.read_storyboard(unit)
    task = build_task('sketch', shot_id=shot, offset_ms=0, board=current, style={'text': '低饱和木刻'})
    assert set(task) == {'kind', 'shot_id', 'offset_ms', 'layout', 'moment', 'assets', 'style',
                         'constraints', 'expect', 'prompt'}
    assert (task['kind'], task['shot_id'], task['offset_ms']) == ('sketch', shot, 0)
    # 缺省用该帧已存框图（已规范化：数值为 float，带哈希）。
    assert task['layout']['objects'][0]['bbox'] == [0.1, 0.2, 0.4, 0.9]
    assert task['layout']['hash'] == current['shots'][0]['timeline']['keyframes'][0]['layout']['hash']
    assert task['moment'] == {'name': '递伞', 'description': '手指接触'}
    assert task['assets'] == [] and task['style'] == {'text': '低饱和木刻', 'references': []}
    assert task['constraints'] == {'aspect': '16:9', 'resolution': None, 'duration_ms': 3000,
                                   'first_frame': None, 'last_frame': None, 'source_image': None}
    assert task['expect'] == {'bind_to': 'keyframe_sketch',
                              'receipt_fields': ['sketch_path', 'sketch_source', 'sketch_basis_hash']}
    assert task['prompt'] == ''
    # 摊成说明时写出机位、主体框与时刻，不补剧情。
    prompt = task_prompt(task)
    assert '递伞' in prompt and 'bbox' in prompt and 'medium' in prompt
    # 显式给了 layout 就覆盖存储值；moment 也能覆盖。
    overridden = build_task('sketch', shot_id=shot, offset_ms=0, board=current, layout=BOX | {'source': 'previs'},
                            moment={'name': '抬手'}, prompt='多一些留白')
    assert overridden['layout']['source'] == 'previs'
    assert overridden['moment'] == {'name': '抬手', 'description': '手指接触'}
    assert overridden['prompt'] == '多一些留白'


def test_build_task_rejects_bad_input(scene):
    ops, unit, shot, board = scene
    current = ops.read_storyboard(unit)
    with pytest.raises(TaskInputError, match='生成类型'):
        build_task('animatic', shot_id=shot, offset_ms=0, board=current)
    with pytest.raises(TaskInputError, match='不属于本场'):
        build_task('sketch', shot_id='shot-999', offset_ms=0, board=current)
    with pytest.raises(TaskInputError, match='还没有关键帧'):
        build_task('sketch', shot_id=shot, offset_ms=1200, board=current)
    task = build_task('sketch', shot_id=shot, offset_ms=0, board=current)
    with pytest.raises(TaskInputError, match='不能改绑定目标'):
        build_task('sketch', shot_id=shot, offset_ms=0, board=current,
                   expect={'bind_to': 'style_path'})
    with pytest.raises(TaskInputError, match='assets'):
        build_task('sketch', shot_id=shot, offset_ms=0, board=current, assets=[{'role': 'reference'}])
    with pytest.raises(TaskInputError, match='constraints'):
        build_task('sketch', shot_id=shot, offset_ms=0, board=current, constraints={'fps': 24})
    assert task['kind'] == 'sketch'


def test_style_task_needs_a_bound_sketch_and_carries_it(scene):
    ops, unit, shot, board = scene
    current = ops.read_storyboard(unit)
    task = build_task('style', shot_id=shot, offset_ms=0, board=current)
    sketch = current['shots'][0]['keyframes'][0]['sketch_path']
    assert task['expect']['bind_to'] == 'keyframe_style'
    assert task['constraints']['source_image'] == sketch
    # 该帧在、但草图没绑：风格帧没有输入图，直接说清楚。
    unbound = {**current, 'shots': [{**current['shots'][0], 'keyframes': [
        dict(current['shots'][0]['keyframes'][0], sketch_path='')]}]}
    with pytest.raises(TaskInputError, match='已绑定的草图'):
        build_task('style', shot_id=shot, offset_ms=0, board=unbound)


def test_video_task_derives_endpoints_from_style_frames(scene, tmp_path):
    ops, unit, shot, board = scene
    style = tmp_path / 'style.png'
    Image.new('RGB', (16, 9), 'gray').save(style)
    ops.apply_storyboard_changes(unit, board['revision'], [{'op': 'set_approved', 'approved': True}])
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'], [
        {'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 0, 'style_path': str(style)}])
    current = ops.read_storyboard(unit)
    task = build_task('video', shot_id=shot, offset_ms=0, board=current)
    assert task['expect'] == {'bind_to': 'shot_generations', 'receipt_fields': ['generations']}
    assert task['constraints']['first_frame'] == current['shots'][0]['keyframes'][0]['style_path']
    assert task['constraints']['last_frame'] is None


# ------------------------------------------------------------------ 技能选择
def test_select_skill_falls_back_to_builtin_generation_skills(tmp_path, monkeypatch):
    """没有用户技能时不是报错：内置三条就是开箱即用的默认（回执里会标 builtin）。"""
    for kind in generation_task.KINDS:
        picked = select_skill(kind)
        assert picked.builtin is True and picked.workflow['kind'] == kind
    assert select_skill('sketch').name == '素描（FLUX 3 Klein）'
    assert select_skill('sketch').workflow['model'] == 'flux3-image-klein'
    assert select_skill('video').workflow['model'] == 'MiniMax-H3'

    # 连内置都被裁掉（例如打包漏了技能模块）才报错，并把当前启用的生成技能列出来。
    monkeypatch.setattr('narrative.skills.BUILTIN_SKILLS', ())
    with pytest.raises(TaskInputError) as error:
        select_skill('sketch')
    assert '没有可用于sketch生成的技能' in str(error.value) and '无' in str(error.value)
    write(tmp_path / 'skills.json', skill('风格帧', 'style'))
    with pytest.raises(TaskInputError) as error:
        select_skill('sketch')
    assert '风格帧（style）' in str(error.value)
    with pytest.raises(TaskInputError, match='生成类型'):
        select_skill('animatic')


def test_select_skill_takes_the_most_recently_updated_and_skips_disabled(tmp_path):
    write(tmp_path / 'skills.json',
          skill('旧草图', 'sketch', stamp='2026-01-01T00:00:00Z'),
          skill('新草图', 'sketch', stamp='2026-02-01T00:00:00Z'),
          skill('停用草图', 'sketch', enabled=False, stamp='2026-03-01T00:00:00Z'),
          skill('风格帧', 'style', stamp='2026-04-01T00:00:00Z'))
    assert select_skill('sketch').name == '新草图'
    assert select_skill('style').name == '风格帧'
    # 用户技能一律压过内置：同 kind 只要有启用的一条，就不会去用默认那条。
    assert select_skill('sketch').builtin is False and select_skill('style').builtin is False

    # 用户的同 kind 技能全停用 = 回到内置默认，而不是报错。
    write(tmp_path / 'skills.json', skill('停用草图', 'sketch', enabled=False))
    assert select_skill('sketch').builtin is True
    # 没声明 workflow 的技能不算生成技能，也不会顶替内置。
    write(tmp_path / 'skills.json', Skill(name='纯说明', summary='s', instructions='i'))
    assert select_skill('style').builtin is True


# ------------------------------------------------------------------ 执行器
def test_execute_returns_receipt_and_passes_what_it_can(monkeypatch, tmp_path):
    calls = []
    fake_sketch(monkeypatch, calls=calls)
    monkeypatch.delenv('NARRATIVE_COMFY_SKETCH_WORKFLOW', raising=False)
    workflow_file = tmp_path / 'klein.json'
    workflow_file.write_text('{}', encoding='utf-8')
    picked = skill('草图', 'sketch', params={'steps': 30}, workflow_file=str(workflow_file))
    task = {'kind': 'sketch', 'shot_id': 'shot-1', 'offset_ms': 0, 'layout': None,
            'moment': {'name': '递伞', 'description': ''},
            'assets': [], 'style': {'text': '木刻', 'references': []},
            'constraints': {'aspect': '16:9', 'resolution': None, 'duration_ms': 2000,
                            'first_frame': None, 'last_frame': None, 'source_image': None},
            'expect': {'bind_to': 'keyframe_sketch', 'receipt_fields': ['sketch_path']}, 'prompt': '留白'}
    receipt = execute(task, picked, work_dir=tmp_path / 'out')
    assert set(receipt) == {'task', 'skill', 'provider_receipt', 'warnings', 'started_at', 'finished_at',
                            'output_path'}
    # 用户技能跑出来的回执标 builtin=false：界面与助手要能分清「用户配的」和「默认的」。
    assert receipt['skill'] == {'name': '草图', 'model': 'stub-model', 'license': '',
                                'expectations': {}, 'builtin': False}
    assert receipt['provider_receipt']['job_id'] == 'job-9'
    assert receipt['started_at'].endswith('Z') and receipt['finished_at'].endswith('Z')
    assert Path(receipt['output_path']).is_file() and calls[0]['guide_exists']
    assert '留白' in calls[0]['prompt'] and '递伞' in calls[0]['prompt']
    # params 没有透传通道：记 warning，不假装用上了；workflow_file 通过环境变量真的生效，退出即还原。
    assert {row['code'] for row in receipt['warnings']} == {'WORKFLOW_PARAMS_UNSUPPORTED'}
    assert calls[0]['workflow'] == str(workflow_file)
    assert os.environ.get('NARRATIVE_COMFY_SKETCH_WORKFLOW') is None


def test_execute_marks_builtin_and_resolves_the_shipped_workflow(monkeypatch, tmp_path):
    """回落用内置技能跑：回执标 builtin=true，随程序发布的工作流文件真的生效。"""
    calls = []
    fake_sketch(monkeypatch, calls=calls)
    monkeypatch.delenv('NARRATIVE_COMFY_SKETCH_WORKFLOW', raising=False)
    picked = select_skill('sketch')
    assert picked.builtin is True
    task = {'kind': 'sketch', 'shot_id': 'shot-1', 'offset_ms': 0, 'layout': None, 'moment': {},
            'assets': [], 'style': {'text': '木刻', 'references': []},
            'constraints': {'duration_ms': 2000, 'source_image': None},
            'expect': {}, 'prompt': ''}
    receipt = execute(task, picked, work_dir=tmp_path / 'builtin')
    assert receipt['skill']['builtin'] is True
    assert receipt['skill']['name'] == '素描（FLUX 3 Klein）'
    assert receipt['skill']['model'] == 'flux3-image-klein'
    assert receipt['skill']['license'].startswith('FLUX Kommunity')
    assert receipt['skill']['expectations'] == {'aspect_from_shot': True, 'reference_images_max': 10}
    # 相对路径按包目录解析：随程序发布的那份文件被设进了环境变量，退出即还原。
    assert calls[0]['workflow'].endswith(str(Path('workflows') / 'storyboard_flux_klein.json'))
    assert os.environ.get('NARRATIVE_COMFY_SKETCH_WORKFLOW') is None
    # 文件存在、也没有 params：这一轮没有「声明了却没生效」的字段。
    assert receipt['warnings'] == []


def test_execute_builtin_style_reports_the_absent_workflow_file(monkeypatch, tmp_path):
    """Krea 2 那条还没带工作流文件：缺声明是已知状态，要在 warnings 里说出来。"""
    fake_style(monkeypatch)
    source = tmp_path / 'sketch.png'
    source.write_bytes(b'placeholder')
    task = {'kind': 'style', 'shot_id': 'shot-1', 'offset_ms': 0, 'layout': None, 'moment': {},
            'assets': [], 'style': {'text': '木刻', 'references': []},
            'constraints': {'duration_ms': 2000, 'source_image': str(source)},
            'expect': {}, 'prompt': ''}
    receipt = execute(task, select_skill('style'), work_dir=tmp_path / 'builtin-style')
    assert receipt['skill']['builtin'] is True and receipt['skill']['model'] == 'krea-2'
    assert [row['code'] for row in receipt['warnings']] == ['WORKFLOW_FILE_ABSENT']


def test_execute_rejects_a_skill_of_another_kind(monkeypatch, tmp_path):
    fake_sketch(monkeypatch)
    task = {'kind': 'sketch', 'shot_id': 'shot-1', 'offset_ms': 0, 'layout': None, 'moment': {},
            'assets': [], 'style': {'text': '', 'references': []},
            'constraints': {'duration_ms': 2000, 'source_image': None},
            'expect': {}, 'prompt': ''}
    with pytest.raises(TaskInputError, match='不一致'):
        execute(task, skill('风格帧', 'style'), work_dir=tmp_path)


def test_execute_wraps_provider_failure_as_service_error(monkeypatch, tmp_path):
    fake_sketch(monkeypatch, fail='ComfyUI 连接失败')
    task = {'kind': 'sketch', 'shot_id': 'shot-1', 'offset_ms': 0, 'layout': None, 'moment': {},
            'assets': [], 'style': {'text': '', 'references': []},
            'constraints': {'duration_ms': 2000, 'source_image': None},
            'expect': {}, 'prompt': ''}
    with pytest.raises(TaskProviderError) as error:
        execute(task, skill('草图', 'sketch'), work_dir=tmp_path)
    assert '生成服务调用失败' in str(error.value) and 'ComfyUI 连接失败' in str(error.value)
    # 产物无效也算服务失败：写出来的不是图就不回执成功。
    def broken(guide, prompt, copies_to, reference_images=()):
        Path(copies_to).write_bytes(b'not an image')
        return {'provider': 'fake', 'job_id': 'job-broken'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', broken)
    with pytest.raises(TaskProviderError, match='没有返回可读图片'):
        execute(task, skill('草图', 'sketch'), work_dir=tmp_path / 'again')


# ------------------------------------------------------------------ 端到端（Operations）
def run(ops, unit, kind, shot, **extra):
    board = ops.read_storyboard(unit)
    return ops.run_generation_task(unit, board['revision'], kind, shot, extra.pop('offset_ms', 0), **extra)


def test_run_generation_task_binds_the_sketch(scene, monkeypatch, tmp_path):
    ops, unit, shot, board = scene
    write(tmp_path / 'skills.json', skill('草图', 'sketch'))
    fake_sketch(monkeypatch)
    result = run(ops, unit, 'sketch', shot, prompt='窗边的光更冷')
    assert result['bound_to'] == 'keyframe_sketch' and result['storyboard_revision'] == board['revision'] + 1
    assert Path(result['output_path']).is_file()
    assert result['provider']['job_id'] == 'job-9' and result['warnings'] == []
    assert result['task']['prompt'] == '窗边的光更冷'
    current = ops.read_storyboard(unit)
    frame = current['shots'][0]['keyframes'][0]
    # 结果绑定在同一个关键帧上，并记下当时的依据哈希。
    assert frame['sketch_path'] == result['output_path'] and frame['sketch_basis_hash']
    assert frame['sketch_source']['user_prompt'] == '窗边的光更冷'


def test_run_generation_task_failure_keeps_the_old_image(scene, monkeypatch, tmp_path):
    ops, unit, shot, board = scene
    write(tmp_path / 'skills.json', skill('草图', 'sketch'))
    fake_sketch(monkeypatch)
    first = run(ops, unit, 'sketch', shot)
    before = ops.read_storyboard(unit)
    fake_sketch(monkeypatch, fail='ComfyUI 服务未启动')
    with pytest.raises(TaskProviderError, match='生成服务调用失败'):
        run(ops, unit, 'sketch', shot)
    assert ops.read_storyboard(unit) == before
    assert before['shots'][0]['keyframes'][0]['sketch_path'] == first['output_path']


def test_run_generation_task_falls_back_to_the_builtin_skill(scene, monkeypatch):
    """工具链路同样回落：用户没配技能时用内置素描，结果照常绑定并标 builtin。"""
    ops, unit, shot, board = scene
    fake_sketch(monkeypatch)
    result = run(ops, unit, 'sketch', shot)
    assert result['skill']['builtin'] is True
    assert result['skill']['name'] == '素描（FLUX 3 Klein）'
    assert result['bound_to'] == 'keyframe_sketch' and Path(result['output_path']).is_file()
    assert result['warnings'] == []


def test_run_generation_task_explains_when_no_skill_declares_the_kind(scene, monkeypatch):
    ops, unit, shot, board = scene
    fake_sketch(monkeypatch)
    # 内置技能被裁掉、用户也没配：这才是「技能缺失」，错误要说清可选什么。
    monkeypatch.setattr('narrative.skills.BUILTIN_SKILLS', ())
    before = ops.read_storyboard(unit)
    with pytest.raises(TaskInputError, match='没有可用于sketch生成的技能'):
        run(ops, unit, 'sketch', shot)
    assert ops.read_storyboard(unit) == before


def test_run_generation_task_binds_style_only_when_approved(scene, monkeypatch, tmp_path):
    ops, unit, shot, board = scene
    write(tmp_path / 'skills.json', skill('风格帧', 'style'))
    fake_style(monkeypatch)
    ops.save_project_style('低饱和木刻版画', 0)
    # 未定案：风格帧要先定案，错误在调用服务之前就给出来。
    with pytest.raises(ValueError, match='先定案'):
        run(ops, unit, 'style', shot)
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'], [{'op': 'set_approved', 'approved': True}])
    result = run(ops, unit, 'style', shot)
    assert result['bound_to'] == 'keyframe_style'
    frame = ops.read_storyboard(unit)['shots'][0]['keyframes'][0]
    assert frame['style_path'] == result['output_path'] and Path(frame['style_path']).is_file()


def test_run_generation_task_video_needs_endpoints(scene, monkeypatch, tmp_path):
    ops, unit, shot, board = scene
    write(tmp_path / 'skills.json', skill('视频', 'video', model='MiniMax-H3'))
    before = ops.read_storyboard(unit)
    with pytest.raises(TaskInputError, match='首尾帧'):
        run(ops, unit, 'video', shot)
    assert ops.read_storyboard(unit) == before


def test_run_generation_task_video_submits_endpoints_into_the_receipt_table(scene, monkeypatch, tmp_path):
    ops, unit, shot, board = scene
    write(tmp_path / 'skills.json', skill('视频', 'video', model='MiniMax-H3'))
    style = tmp_path / 'style.png'
    Image.new('RGB', (768, 432), 'gray').save(style)
    ops.apply_storyboard_changes(unit, board['revision'], [{'op': 'set_approved', 'approved': True}])
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'], [
        {'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 0, 'style_path': str(style)}])
    submitted = {}

    def submit(base_url, api_key, request):
        submitted.update({'base_url': base_url, 'api_key': api_key, 'request': request})
        return {'task_id': 'task-1', 'task': {'id': 'task-1', 'status': 'queued'}}

    monkeypatch.setattr('narrative.storyboard_video_provider.submit', submit)
    monkeypatch.setenv('MINIMAX_API_KEY', 'secret-key')
    result = run(ops, unit, 'video', shot, constraints={'duration_ms': 5000})
    # 首尾帧模式：只发首帧（本镜只有一张风格帧），时长用显式选择的 5 秒。
    assert submitted['request']['duration'] == 5
    assert [item.get('role') for item in submitted['request']['content'] if 'role' in item] == ['first_frame']
    assert result['provider']['task_id'] == 'task-1' and result['bound_to'] == 'shot_generations'
    generation = ops.read_storyboard(unit)['shots'][0]['generations'][-1]
    assert generation['status'] == 'submitted' and generation['mode'] == 'endpoints'
    assert generation['provider_task_id'] == 'task-1' and generation['basis_hash']
    assert Path(generation['request_path']).is_file() and generation['request_sha256']
    # 密钥只用于提交，不进回执。
    assert 'secret-key' not in json.dumps(result, ensure_ascii=False)


def test_run_generation_task_rejects_unknown_offset_and_stale_revision(scene, monkeypatch, tmp_path):
    ops, unit, shot, board = scene
    write(tmp_path / 'skills.json', skill('草图', 'sketch'))
    fake_sketch(monkeypatch)
    with pytest.raises(ValueError, match='还没有关键帧'):
        run(ops, unit, 'sketch', shot, offset_ms=1500)
    with pytest.raises(ValueError, match='刷新'):
        ops.run_generation_task(unit, board['revision'] + 5, 'sketch', shot, 0)
    with pytest.raises(ValueError, match='生成类型'):
        run(ops, unit, 'animatic', shot)


def test_generation_tools_are_registered():
    assert tool_api.TOOLS['read_storyboard_layout'].writes is False
    assert tool_api.TOOLS['run_generation_task'].writes is True
    assert set(tool_api.TOOLS['run_generation_task'].params) == {
        'unit_id', 'revision', 'kind', 'shot_id', 'offset_ms', 'prompt', 'layout', 'moment', 'constraints'}
    assert tool_api.TOOLS['run_generation_task'].params['kind']['enum'] == ['sketch', 'style', 'video']
    # 资产的两类走自己的工具（run_asset_generation_task），与分镜的 kind 同一份总表。
    assert generation_task.KINDS == ('sketch', 'style', 'video', 'asset_design', 'asset_sketch')
