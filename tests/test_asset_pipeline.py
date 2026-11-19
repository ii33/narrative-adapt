"""资产两级产物流水线：文字稿→设定图→素描图，以及它们在分镜里的参考链路。

用假的图像 provider（monkeypatch render_sketch_image / render_style_image）跑，不联网、
不碰真实 ComfyUI；技能文件一律指到 tmp_path，绝不碰 ~/.narrative/skills.json。
"""
import hashlib
from pathlib import Path

import pytest
from agent_fixtures import fresh_project
from PIL import Image

from narrative import generation_task
from narrative.generation_task import (
    TaskInputError,
    TaskProviderError,
    build_task,
    select_skill,
    task_prompt,
)
from narrative.skills import Skill, save_skills


@pytest.fixture(autouse=True)
def skills_file(tmp_path, monkeypatch):
    path = tmp_path / 'skills.json'
    monkeypatch.setenv('NARRATIVE_SKILLS_FILE', str(path))
    return path


@pytest.fixture
def ops(tmp_path, monkeypatch, project_template):
    """一份空工程副本：这些用例只写资产库与风格约定，不需要剧本链条。"""
    return fresh_project(tmp_path, monkeypatch, template=project_template)


def skill(name, kind, *, model='stub-model', enabled=True, stamp='2026-01-01T00:00:00Z', **workflow):
    return Skill(name=name, summary='生成一次', instructions='按任务包生成', enabled=enabled,
                 created_at=stamp, updated_at=stamp, workflow={'kind': kind, 'model': model, **workflow})


def write(path, *rows):
    save_skills(list(rows), path)
    return path


def fake_design(monkeypatch, *, fail=None, calls=None):
    """假的设定图 provider：每次写不同像素，好让 content_hash 真的会变。"""
    state = {'n': 0}

    def render(canvas, prompt, copies_to, reference_images=()):
        if calls is not None:
            calls.append({'guide': str(canvas), 'prompt': prompt,
                          'references': [str(path) for path in reference_images]})
        if fail:
            raise ValueError(fail)
        state['n'] += 1
        Image.new('RGB', (24, 36), (state['n'] * 40, 0, 0)).save(copies_to)
        return {'provider': 'fake-comfy', 'job_id': f'job-design-{state["n"]}'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_style_image', render)
    return state


def fake_sketch(monkeypatch, *, fail=None, calls=None):
    state = {'n': 0}

    def render(guide, prompt, copies_to, reference_images=()):
        if calls is not None:
            calls.append({'guide': str(guide), 'prompt': prompt,
                          'references': [str(path) for path in reference_images]})
        if fail:
            raise ValueError(fail)
        state['n'] += 1
        Image.new('RGB', (24, 36), (0, state['n'] * 40, 0)).save(copies_to)
        return {'provider': 'fake-comfy', 'job_id': f'job-sketch-{state["n"]}'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)
    return state


# ------------------------------------------------------------------ 文字稿
def test_write_asset_draft_replaces_and_bumps_revision(ops):
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    first = ops.write_asset_draft(card['id'], {'costume': '灰布短褂', 'appearance': '瘦高，短发'},
                                  [{'topic': '1930 年代江南学生装',
                                    'note': '灰布短褂配深色长裤，学生不佩饰物',
                                    'basis': '地方志与同期照片'}])
    assert first['draft_revision'] == 1 and first['asset_revision'] == card['revision'] + 1
    # 段落按白名单顺序落库：同一份文字稿因此总是同一份 JSON。
    assert list(first['draft']['sections']) == ['appearance', 'costume']
    assert first['draft']['sections'] == {'appearance': '瘦高，短发', 'costume': '灰布短褂'}
    # 考据记的是外部依据（查到了什么），不是作品原文引用。
    assert first['draft']['research'] == [{'topic': '1930 年代江南学生装',
                                           'note': '灰布短褂配深色长裤，学生不佩饰物',
                                           'basis': '地方志与同期照片'}]
    # 写稿时的风格与约定版本由程序记，不接受手填。
    assert first['style_ref'] == {'project_style_version': 0, 'agreement_version': 0}
    # 与资产卡里其它时间（set.updated_at）同一口径：UTC 带显式偏移。
    assert first['draft']['updated_at'].endswith('+00:00')

    second = ops.write_asset_draft(card['id'], {'proportion': '七头身，肩窄'})
    assert second['draft_revision'] == 2
    # 整份替换：旧段落与旧考据都不沿用，要留就一起重写。
    assert second['draft']['sections'] == {'proportion': '七头身，肩窄'}
    # 考据是可选的：不写就是空表，也不影响文字稿成立。
    assert second['draft']['research'] == []
    stored = ops.get_asset(card['id'])['draft']
    assert stored == second['draft']
    # 改过风格约定后再写稿，记的是当时的版本。
    ops.save_project_style('低饱和木刻版画', 0)
    third = ops.write_asset_draft(card['id'], {'appearance': '瘦高'})
    assert third['style_ref'] == {'project_style_version': 1, 'agreement_version': 1}


def test_write_asset_draft_rejects_bad_input(ops):
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    with pytest.raises(ValueError, match='note须为非空文本'):
        ops.write_asset_draft(card['id'], {'appearance': '瘦高'}, [{'topic': '弄堂门朝哪开'}])
    with pytest.raises(ValueError, match='不认识的段落'):
        ops.write_asset_draft(card['id'], {'props': '黑伞'})
    with pytest.raises(ValueError, match='appearance资产不写location段'):
        ops.write_asset_draft(card['id'], {'location': '码头'})
    with pytest.raises(ValueError, match='超过2000字'):
        ops.write_asset_draft(card['id'], {'appearance': '长' * 2001})
    with pytest.raises(ValueError, match='至少写一段'):
        ops.write_asset_draft(card['id'], {})
    with pytest.raises(ValueError, match='research第1条'):
        ops.write_asset_draft(card['id'], {'appearance': '瘦高'}, ['门朝南开'])
    # 每一次拒绝都不落库：卡片仍然没有文字稿。
    assert 'draft' not in ops.get_asset(card['id'])

    # 类别与允许的段落对应：道具能写道具形制，不能写服装。
    item = ops.save_asset({'category': 'item', 'title': '黑伞'})
    with pytest.raises(ValueError, match='item资产不写costume段'):
        ops.write_asset_draft(item['id'], {'costume': '红色'})
    assert ops.write_asset_draft(item['id'], {'prop': '黑布伞，木柄，伞骨十二根'})['draft_revision'] == 1


# ------------------------------------------------------------------ 任务包
def test_asset_design_task_carries_draft_style_and_aspect(ops):
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    ops.save_project_style('低饱和木刻版画', 0)
    ops.write_asset_draft(card['id'], {'appearance': '瘦高，短发', 'costume': '灰布短褂'})
    current = ops.get_asset(card['id'])
    task = build_task('asset_design', asset=current, style={'text': '低饱和木刻版画'}, prompt='正面站姿')
    assert set(task) == {'kind', 'asset_id', 'title', 'category', 'draft', 'design', 'view',
                         'style', 'constraints', 'expect', 'prompt'}
    assert (task['kind'], task['asset_id'], task['title']) == ('asset_design', card['id'], '少年')
    # 任务包里只有段落与修订号：考据与版本留在卡片上，提示词不该让模型去引原文。
    assert task['draft'] == {'sections': {'appearance': '瘦高，短发', 'costume': '灰布短褂'}, 'revision': 1}
    assert task['design'] is None and task['view'] == 'front'
    # 只声明程序真的能约束的项：图像 provider 只认输入图画布，分辨率没有通道。
    assert task['constraints'] == {'aspect': '2:3', 'source_image': None}
    assert task['expect'] == {'bind_to': 'asset_designs', 'receipt_fields': ['designs']}
    assert task['style'] == {'text': '低饱和木刻版画', 'references': []}
    prompt = task_prompt(task)
    assert '文字稿（第1版）' in prompt and '灰布短褂' in prompt
    assert '低饱和木刻版画' in prompt and '2:3' in prompt and '正面站姿' in prompt

    # 画幅按类别给：道具见形制用 1:1，地点看空间关系用 16:9；也能显式覆盖。
    assert build_task('asset_design', asset={'id': 'i-1', 'category': 'item'})['constraints']['aspect'] == '1:1'
    assert build_task('asset_design', asset={'id': 'l-1', 'category': 'location'})['view'] == 'none'
    assert build_task('asset_design', asset=current, constraints={'aspect': '1:1'})['constraints']['aspect'] == '1:1'
    assert build_task('asset_design', asset=current, view='side')['view'] == 'side'


def test_asset_task_rejects_bad_input(ops):
    card = ops.get_asset(ops.save_asset({'category': 'appearance', 'title': '少年'})['id'])
    with pytest.raises(TaskInputError, match='asset_id与资产卡不一致'):
        build_task('asset_design', asset=card, asset_id='appearance-99')
    with pytest.raises(TaskInputError, match='需要资产卡数据'):
        build_task('asset_design')
    with pytest.raises(TaskInputError, match='资产类别'):
        build_task('asset_design', asset={'id': 'x-1', 'category': 'weird'})
    with pytest.raises(TaskInputError, match='constraints'):
        build_task('asset_design', asset=card, constraints={'view': 'front'})
    with pytest.raises(TaskInputError, match='aspect须为'):
        build_task('asset_design', asset=card, constraints={'aspect': '4:5'})
    with pytest.raises(TaskInputError, match='view须为'):
        build_task('asset_design', asset=card, view='top')
    with pytest.raises(TaskInputError, match='生成类型'):
        build_task('animatic', asset=card)


def test_asset_sketch_takes_the_front_design_and_the_sketch_style(ops, tmp_path):
    card = ops.get_asset(ops.save_asset({'category': 'appearance', 'title': '少年'})['id'])
    # 还没有设定图：素描图必须以一张设定图为依据，缺了就在调服务之前报错。
    with pytest.raises(TaskInputError, match='还没有设定图'):
        build_task('asset_sketch', asset=card, sketch_style='线条为主')
    front = {'file': 'x/front.png', 'path': str(tmp_path / 'front.png'), 'kind': 'figure', 'view': 'front',
             'draft_revision': 1}
    side = {'file': 'x/side.png', 'path': str(tmp_path / 'side.png'), 'kind': 'figure', 'view': 'side',
            'draft_revision': 2}
    with_designs = card | {'designs': [front, side]}
    task = build_task('asset_sketch', asset=with_designs, sketch_style='线条为主，留白')
    # 优先 view=front：跨镜一致最需要的是正面那张的形体，不是列表首项碰巧是哪张。
    assert task['design'] == front
    assert task['constraints']['source_image'] == front['path']
    assert task['sketch_style'] == '线条为主，留白' and task['view'] == 'front'
    assert task['expect'] == {'bind_to': 'asset_sketches', 'receipt_fields': ['sketches']}
    assert '线条为主，留白' in task_prompt(task) and '设定图依据（front视角' in task_prompt(task)
    # 显式指定就按指定的；指到别的资产上要报错，不能悄悄换成另一张。
    assert build_task('asset_sketch', asset=with_designs, design_path=side['path'])['design'] == side
    with pytest.raises(TaskInputError, match='不属于这张资产'):
        build_task('asset_sketch', asset=with_designs, design_path=str(tmp_path / 'other.png'))
    # 没有正面时取最近一张，而不是最早那张。
    assert build_task('asset_sketch', asset=card | {'designs': [side]})['design'] == side


# ------------------------------------------------------------------ 技能与回落
def test_asset_kinds_fall_back_to_builtin_skills():
    for kind in ('asset_design', 'asset_sketch'):
        picked = select_skill(kind)
        assert picked.builtin is True and picked.workflow['kind'] == kind
        assert '待确认' in picked.workflow['license']
    assert select_skill('asset_design').workflow['model'] == 'krea-2'
    assert select_skill('asset_sketch').workflow['model'] == 'flux3-image-klein'
    # 与分镜同一批模型：资产与关键帧该像一家人，不另起一套。
    assert select_skill('asset_design').workflow['model'] == select_skill('style').workflow['model']
    assert select_skill('asset_sketch').workflow['model'] == select_skill('sketch').workflow['model']


def test_asset_skill_selection_prefers_the_user_declaration(skills_file, monkeypatch):
    write(skills_file, skill('设定图', 'asset_design', model='local/design-v1'))
    picked = select_skill('asset_design')
    assert picked.name == '设定图' and picked.builtin is False
    # 素描那条用户没声明：仍然回落到内置，而不是报错。
    assert select_skill('asset_sketch').builtin is True
    # 连内置都被裁掉才报错，并把当前启用的生成技能列出来。
    monkeypatch.setattr('narrative.skills.BUILTIN_SKILLS', ())
    with pytest.raises(TaskInputError) as error:
        select_skill('asset_sketch')
    assert '没有可用于asset_sketch生成的技能' in str(error.value)


# ------------------------------------------------------------------ 执行与写回
def test_run_asset_generation_records_design_then_sketch(ops, monkeypatch):
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    ops.save_project_style('低饱和木刻版画', 0)
    ops.write_asset_draft(card['id'], {'appearance': '瘦高，短发'})
    design_calls, sketch_calls = [], []
    fake_design(monkeypatch, calls=design_calls)
    design = ops.run_asset_generation_task(card['id'], 'asset_design', prompt='正面站姿')
    record = design['record']
    assert design['skill']['builtin'] is True and design['skill']['model'] == 'krea-2'
    assert Path(record['path']).is_file()
    assert (record['kind'], record['view']) == ('figure', 'front')
    assert (record['draft_revision'], record['style_version']) == (1, 1)
    assert (record['provider'], record['job_id']) == ('fake-comfy', 'job-design-1')
    assert record['prompt'].startswith('资产：appearance／少年') and '正面站姿' in record['prompt']
    assert design['asset_revision'] == card['revision'] + 2
    # 内置设定图技能没有随程序发布的工作流文件：这条已知状态要说出来，不假装用上了。
    assert [row['code'] for row in design['warnings']] == ['WORKFLOW_FILE_ABSENT']
    assert design_calls[0]['guide'].endswith('asset-canvas.png')
    # 产物复制进资产目录，原件在临时目录里已被回收也还读得到。
    assert 'asset-design-1' in record['path']
    assert Path(record['path']).read_bytes()

    # 素描图：以设定图为输入，记录依据的是哪张设定图。
    fake_sketch(monkeypatch, calls=sketch_calls)
    sketch = ops.run_asset_generation_task(card['id'], 'asset_sketch')
    drawn = sketch['record']
    assert sketch['skill']['builtin'] is True and sketch['skill']['model'] == 'flux3-image-klein'
    assert sketch['skill']['expectations'] == {'source_design_required': True, 'reference_images_max': 10}
    assert sketch_calls[0]['guide'] == record['path']
    assert (drawn['kind'], drawn['view']) == ('figure', 'front')
    assert Path(drawn['design_path']).resolve() == Path(record['path']).resolve()
    assert sketch['warnings'] == []
    assert Path(drawn['path']).is_file() and drawn['path'] != record['path']
    # 卡片上两个列表分开留着，追加而不覆盖。
    current = ops.get_asset(card['id'])
    assert [row['path'] for row in current['designs']] == [record['path']]
    assert [row['path'] for row in current['sketches']] == [drawn['path']]
    assert current['draft']['revision'] == 1


def test_user_skill_runs_the_asset_design_without_builtin_warnings(skills_file, ops, monkeypatch):
    card = ops.save_asset({'category': 'item', 'title': '黑伞'})
    ops.write_asset_draft(card['id'], {'prop': '黑布伞，木柄，伞骨十二根'})
    write(skills_file, skill('设定图', 'asset_design', model='local/design-v1'))
    fake_design(monkeypatch)
    result = ops.run_asset_generation_task(card['id'], 'asset_design')
    assert result['skill']['builtin'] is False and result['skill']['model'] == 'local/design-v1'
    # 有文字稿、用户技能也没声明 workflow_file：这一轮没有「声明了却没生效」的字段。
    assert result['warnings'] == []
    assert result['record']['kind'] == 'prop'
    # 用户没声明 asset_sketch：那条仍然回落到内置，不是报错。
    fake_sketch(monkeypatch)
    assert ops.run_asset_generation_task(card['id'], 'asset_sketch')['skill']['builtin'] is True


def test_asset_design_without_draft_warns_but_still_writes(ops, monkeypatch):
    card = ops.save_asset({'category': 'item', 'title': '黑伞'})
    fake_design(monkeypatch)
    result = ops.run_asset_generation_task(card['id'], 'asset_design')
    codes = [row['code'] for row in result['warnings']]
    # 不设强制闸门：图照出，但要说清「一致性没有文字依据」。
    assert codes == ['DRAFT_MISSING', 'WORKFLOW_FILE_ABSENT']
    assert result['record']['draft_revision'] == 0
    assert '文字稿：无' in result['record']['prompt']
    assert [row['kind'] for row in ops.get_asset(card['id'])['designs']] == ['prop']


def test_asset_sketch_without_design_fails_before_calling_the_service(ops, monkeypatch):
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    calls = []
    fake_sketch(monkeypatch, calls=calls)
    with pytest.raises(ValueError, match='还没有设定图'):
        ops.run_asset_generation_task(card['id'], 'asset_sketch')
    assert calls == [] and 'sketches' not in ops.get_asset(card['id'])
    with pytest.raises(ValueError, match='资产出图类型'):
        ops.run_asset_generation_task(card['id'], 'sketch')
    with pytest.raises(ValueError, match='view须为'):
        ops.run_asset_generation_task(card['id'], 'asset_design', view='top')
    with pytest.raises(ValueError, match='材质资产不生成'):
        ops.run_asset_generation_task(ops.save_asset({'category': 'material', 'title': '铜'})['id'], 'asset_design')


def test_failed_generation_writes_nothing(ops, monkeypatch):
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    fake_design(monkeypatch, fail='ComfyUI 连接失败')
    before = ops.get_asset(card['id'])
    with pytest.raises(TaskProviderError, match='生成服务调用失败'):
        ops.run_asset_generation_task(card['id'], 'asset_design')
    after = ops.get_asset(card['id'])
    assert after == before and 'designs' not in after
    # 服务返回成功但没有可读图片：同样算失败，不写任何产物。
    def broken(canvas, prompt, copies_to, reference_images=()):
        Path(copies_to).write_bytes(b'not an image')
        return {'provider': 'fake', 'job_id': 'job-broken'}
    monkeypatch.setattr('narrative.storyboard_style_provider.render_style_image', broken)
    with pytest.raises(TaskProviderError, match='没有返回可读图片'):
        ops.run_asset_generation_task(card['id'], 'asset_design')
    assert ops.get_asset(card['id']) == before


# ------------------------------------------------------------------ 分镜参考链路
def test_storyboard_basis_tracks_the_design_content_hash(ops, monkeypatch, tmp_path):
    """设定图换了，分镜出图的「依据」必须跟着变：这条确认链路不能断。"""
    card = ops.save_asset({'category': 'appearance', 'title': '少年'})
    ops.write_asset_draft(card['id'], {'appearance': '瘦高，短发'})
    fake_design(monkeypatch)
    first = ops.run_asset_generation_task(card['id'], 'asset_design')['record']
    board = {'unit_id': 'unit-1', 'objects': [{'asset_id': card['id']}], 'shots': [], 'sound_intents': []}
    shot = {'id': 'shot-1', 'visual_elements': [], 'source_block_ids': [], 'keyframes': [],
            'handoff': None, 'sound_notes': '', 'description': '', 'shot_language': ''}
    context = {'unit': {'blocks': [], 'body_revision': 0}, 'project_style': {'text': ''},
               'creative_agreement': {}}
    basis, references = ops._storyboard_sketch_basis(board, shot, 0, context)
    asset = basis['assets'][0]
    assert Path(first['path']) in references
    assert asset['designs'][0]['content_hash'] == hashlib.sha256(Path(first['path']).read_bytes()).hexdigest()

    # 再出一张设定图：新图进参考图，旧图哈希不变，依据整体确实变了（旧草图该重画）。
    second = ops.run_asset_generation_task(card['id'], 'asset_design')['record']
    changed, references_after = ops._storyboard_sketch_basis(board, shot, 0, context)
    hashes_before = {row['path']: row['content_hash'] for row in asset['designs']}
    hashes_after = {row['path']: row['content_hash'] for row in changed['assets'][0]['designs']}
    assert set(hashes_after) - set(hashes_before) == {second['path']}
    assert hashes_after[first['path']] == hashes_before[first['path']]
    assert Path(second['path']) in references_after
    assert changed['assets'] != basis['assets']


def test_asset_image_references_reach_the_task_package(ops):
    """资产图摊成任务包 assets 时，设定图与素描图也算资产图，顺序去重。"""
    cards = [{'id': 'appearance-1', 'images': [{'path': 'a.png'}],
              'designs': [{'path': 'b.png'}, {'path': 'a.png'}], 'sketches': [{'path': 'c.png'}]}]
    assert generation_task.assets_from_cards(cards) == [
        {'asset_id': 'appearance-1', 'role': 'reference', 'images': ['a.png', 'b.png', 'c.png']}]
