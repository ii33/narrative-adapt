"""B1a 每镜内部时间轴 + B1c 跨镜衔接：只做数据合法性校验，派生不落盘。

要点：
* offset_ms 相对镜首；越界、段长上限、段端点不在关键帧、段重叠都要拒绝；
* 改一镜的时间轴不影响其它镜（镜内独立）；
* 整场时间线由各镜拼起来，长度等于各镜时长之和；
* 老数据没有 timeline 时按 duration_ms 与现有关键帧推导，且不改写存储；
* handoff 只能指向同一场里别的镜头。
"""
import json

import pytest
from PIL import Image
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

pytestmark = pytest.mark.slow


@pytest.fixture
def board(project, tmp_path):
    ops, unit, block = setup_board(project)
    image = tmp_path / 'frame.png'
    Image.new('RGB', (16, 9), 'gray').save(image)
    created = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '全景等候', 'duration_ms': 3000, 'source_block_ids': [block]},
        {'op': 'create_shot', 'title': '反打', 'duration_ms': 1500, 'source_block_ids': [block], 'action_start_ms': 3000},
        {'op': 'create_shot', 'title': '空景', 'duration_ms': 500, 'source_block_ids': [block]}])
    frames = [{'op': 'set_keyframe', 'shot_id': shot['id'], 'offset_ms': 0, 'sketch_path': str(image)}
              for shot in created['shots']]
    return ops, unit, ops.apply_storyboard_changes(unit, created['revision'], frames)


def apply(ops, unit, changes):
    board = ops.read_storyboard(unit)
    return ops.apply_storyboard_changes(unit, board['revision'], changes)


def raw_board(ops, unit):
    with ops._store.connect() as db:
        return json.loads(db.execute("SELECT value FROM meta WHERE key=?", ("storyboard:" + unit,)).fetchone()[0])


def plan(timeline):
    """只核对时间轴计划字段：读视图的每一行还带 layout（B2 的框图，没有就是 None），
    那是另一层数据，不该混进「计划里存了什么」的断言。"""
    return [{key: value for key, value in row.items() if key != 'layout'} for row in timeline['keyframes']]


def test_timeline_plan_round_trip_and_shape(board):
    ops, unit, b = board
    shot = b['shots'][0]['id']
    written = apply(ops, unit, [
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 0, 'moment_name': '起手', 'description': '甲抬头'},
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200, 'moment_name': '递伞'},
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 3000},
        {'op': 'set_timeline_segments', 'shot_id': shot, 'segments': [[0, 1200], [1200, 3000]]}])
    timeline = written['shots'][0]['timeline']
    assert timeline['duration_ms'] == 3000
    assert plan(timeline) == [{'offset_ms': 0, 'moment_name': '起手', 'description': '甲抬头'},
                              {'offset_ms': 1200, 'moment_name': '递伞', 'description': ''},
                              {'offset_ms': 3000, 'moment_name': '', 'description': ''}]
    assert timeline['segments'] == [[0, 1200], [1200, 3000]]
    assert written['shots'][0]['timeline_derived'] == {'keyframes': False, 'segments': False}
    # 声音节拍与摄影机关键点按镜内顺序列出（引用，不复制结构）。
    assert timeline['sound_cues'] == [] and timeline['camera_keys'] == []
    # 存储里只存显式计划与显式分段：duration_ms 等派生字段不落盘。
    stored = raw_board(ops, unit)['shots'][0]['timeline']
    assert stored == {'keyframes': [{'offset_ms': 0, 'moment_name': '起手', 'description': '甲抬头'},
                                    {'offset_ms': 1200, 'moment_name': '递伞', 'description': ''},
                                    {'offset_ms': 3000, 'moment_name': '', 'description': ''}],
                      'segments': [[0, 1200], [1200, 3000]]}
    # 改名/描述/移动时刻；空分段表表示回到推导。
    # 显式分段端点必须仍是关键帧：单独挪走 1200 会先被拒，同批给出新分段才通过。
    with pytest.raises(ValueError, match='必须落在已有关键帧上'):
        apply(ops, unit, [{'op': 'update_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200, 'new_offset_ms': 900}])
    renamed = apply(ops, unit, [
        {'op': 'update_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200, 'new_offset_ms': 900,
         'moment_name': '递伞到手', 'description': '手指接触'},
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 2400},
        {'op': 'set_timeline_segments', 'shot_id': shot, 'segments': [[0, 900], [900, 2400], [2400, 3000]]}])
    assert plan(renamed['shots'][0]['timeline'])[1] == {'offset_ms': 900, 'moment_name': '递伞到手',
                                                       'description': '手指接触'}
    cleared = apply(ops, unit, [{'op': 'delete_timeline_keyframe', 'shot_id': shot, 'offset_ms': 900},
                                {'op': 'delete_timeline_keyframe', 'shot_id': shot, 'offset_ms': 2400},
                                {'op': 'delete_timeline_keyframe', 'shot_id': shot, 'offset_ms': 3000},
                                {'op': 'set_timeline_segments', 'shot_id': shot, 'segments': []}])
    assert plan(cleared['shots'][0]['timeline']) == [{'offset_ms': 0, 'moment_name': '起手', 'description': '甲抬头'}]
    assert cleared['shots'][0]['timeline_derived'] == {'keyframes': False, 'segments': True}
    assert cleared['shots'][0]['timeline']['segments'] == [[0, 3000]]


def test_timeline_rejects_out_of_range_and_unknown_offsets(board):
    ops, unit, b = board
    shot = b['shots'][0]['id']
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match='超出'):
        apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 3001}])
    apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 2000}])
    with pytest.raises(ValueError, match='已有计划关键帧'):
        apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 2000}])
    with pytest.raises(ValueError, match='没有该时刻'):
        apply(ops, unit, [{'op': 'update_timeline_keyframe', 'shot_id': shot, 'offset_ms': 10, 'moment_name': 'x'}])
    with pytest.raises(ValueError, match='不属于当前场'):
        apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': 'shot-999', 'offset_ms': 0}])
    # 缩短镜头会挤出已计划的时刻：必须显式调整，不自动截断（内容检查走暂存，整批不写）。
    shrink = ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'],
                                          [{'op': 'update_shot', 'shot_id': shot, 'duration_ms': 1500}])
    assert shrink['status'] == 'staged'
    assert any('超出' in problem['message'] for problem in shrink['problems'])
    assert ops.read_storyboard(unit)['shots'][0]['timeline']['keyframes'][-1]['offset_ms'] == 2000
    assert shot == before['shots'][0]['id']


@pytest.mark.parametrize('segments, message', [
    ([[0, 3001]], '超出'),
    ([[0, 2500]], '单段上限'),
    ([[300, 600]], '必须落在已有关键帧上'),
    ([[0, 1200], [1200, 2000], [1200, 2000]], '不能重叠'),
    ([[1200, 0]], '起点须小于终点'),
])
def test_segment_legality_is_data_only(board, segments, message):
    ops, unit, b = board
    shot = b['shots'][0]['id']
    apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200},
                      {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 2000}])
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match=message):
        apply(ops, unit, [{'op': 'set_timeline_segments', 'shot_id': shot, 'segments': segments}])
    assert ops.read_storyboard(unit) == before


def test_segment_limit_is_configurable_per_scene(board):
    ops, unit, b = board
    shot = b['shots'][0]['id']
    apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 2500}])
    with pytest.raises(ValueError, match='单段上限2000ms'):
        apply(ops, unit, [{'op': 'set_timeline_segments', 'shot_id': shot, 'segments': [[0, 2500]]}])
    # 场级上限可配：写进分镜数据后同一批分段就合法了（默认 2000ms）。
    with ops._store.connect(write=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", ("storyboard:" + unit,)).fetchone()
        data = json.loads(row[0])
        data['timeline_max_segment_ms'] = 4000
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                   ("storyboard:" + unit, json.dumps(data, ensure_ascii=False)))
    written = apply(ops, unit, [{'op': 'set_timeline_segments', 'shot_id': shot, 'segments': [[0, 2500]]}])
    assert written['shots'][0]['timeline']['segments'] == [[0, 2500]]


def test_timeline_is_shot_local_and_scene_total_is_the_sum_of_shots(board):
    ops, unit, b = board
    first, second, third = [shot['id'] for shot in b['shots']]
    apply(ops, unit, [
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 1200, 'moment_name': '递伞'},
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 3000},
        {'op': 'set_timeline_segments', 'shot_id': first, 'segments': [[0, 1200], [1200, 3000]]}])
    now = ops.read_storyboard(unit)
    # 镜内独立：另外两镜的时间轴一点没变。
    assert now['shots'][1]['timeline'] == b['shots'][1]['timeline']
    assert now['shots'][2]['timeline'] == b['shots'][2]['timeline']
    assert 'timeline' not in raw_board(ops, unit)['shots'][1]
    # 整场：每镜分段首尾相接覆盖整镜，拼接长度等于各镜时长之和。
    total = 0
    for shot in now['shots']:
        timeline = shot['timeline']
        assert timeline['segments'][0][0] == 0
        assert timeline['segments'][-1][1] == timeline['duration_ms'] == shot['duration_ms']
        assert all(left[1] == right[0] for left, right in zip(timeline['segments'], timeline['segments'][1:], strict=False))
        total += timeline['duration_ms']
    assert total == sum(shot['duration_ms'] for shot in now['shots']) == 5000
    assert ops.preview_storyboard(unit, 99999)['total_duration_ms'] == total
    assert second and third


def test_old_data_without_timeline_derives_from_keyframes_without_migration(board, tmp_path):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    more = tmp_path / 'more.png'
    Image.new('RGB', (16, 9), 'gray').save(more)
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'],
                                 [{'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 1000, 'sketch_path': str(more)}])
    stored = raw_board(ops, unit)
    assert all('timeline' not in row for row in stored['shots'])
    view = ops.read_storyboard(unit)['shots'][0]
    assert view['timeline_derived'] == {'keyframes': True, 'segments': True}
    # 关键帧计划与分段都按现有关键帧与镜头时长推导。
    assert plan(view['timeline']) == [{'offset_ms': 0, 'moment_name': '', 'description': ''},
                                      {'offset_ms': 1000, 'moment_name': '', 'description': ''}]
    assert view['timeline']['segments'] == [[0, 1000], [1000, 3000]]
    assert view['timeline']['duration_ms'] == 3000
    # 读多少次都不改存储：老数据保持原样。
    assert raw_board(ops, unit) == stored


def test_handoff_points_at_another_shot_in_the_same_scene(board):
    ops, unit, b = board
    first, second = b['shots'][0]['id'], b['shots'][1]['id']
    written = apply(ops, unit, [{'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': first, 'ref_frame': 'last'}])
    assert written['shots'][1]['handoff'] == {'from_shot': first, 'ref_frame': 'last'}
    assert written['shots'][0]['handoff'] is None
    assert apply(ops, unit, [{'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': first, 'ref_frame': 'first'}]
                 )['shots'][1]['handoff']['ref_frame'] == 'first'
    assert apply(ops, unit, [{'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': None}])['shots'][1]['handoff'] is None


@pytest.mark.parametrize('change, message', [
    ({'from_shot': 'shot-999'}, '不属于同一场'),
    ({'from_shot': 'self'}, '本镜自己'),
    ({'from_shot': 'other', 'ref_frame': 'middle'}, 'last'),
])
def test_handoff_rejects_bad_targets_without_writing(board, change, message):
    ops, unit, b = board
    first, second = b['shots'][0]['id'], b['shots'][1]['id']
    target = dict(change, from_shot={'self': second, 'other': first}.get(change['from_shot'], change['from_shot']))
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match=message):
        apply(ops, unit, [{'op': 'set_shot_handoff', 'shot_id': second, **target}])
    assert ops.read_storyboard(unit) == before


def test_deleting_a_handoff_source_must_be_explicit(board):
    ops, unit, b = board
    first, second = b['shots'][0]['id'], b['shots'][1]['id']
    apply(ops, unit, [{'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': first}])
    with pytest.raises(ValueError, match='不属于同一场'):
        apply(ops, unit, [{'op': 'delete_shot', 'shot_id': first}])
    assert len(ops.read_storyboard(unit)['shots']) == 3
    apply(ops, unit, [{'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': None},
                      {'op': 'delete_shot', 'shot_id': first}])
    assert len(ops.read_storyboard(unit)['shots']) == 2


def test_preview_simulates_timeline_edits_without_writing(board):
    ops, unit, b = board
    shot, second = b['shots'][0]['id'], b['shots'][1]['id']
    before = ops.read_storyboard(unit)
    preview = ops.preview_storyboard_changes(unit, b['revision'], [
        {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200, 'moment_name': '递伞'},
        {'op': 'set_timeline_segments', 'shot_id': shot, 'segments': [[0, 1200]]},
        {'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': shot}])
    assert preview['valid'] and preview['conflicts'] == []
    simulated = next(row for row in preview['preview']['shots'] if row['id'] == shot)
    assert simulated['timeline']['keyframes'][-1]['moment_name'] == '递伞'
    assert simulated['timeline']['segments'] == [[0, 1200]]
    # 预览不写库，也不把推导出来的分段当成用户显式值。
    assert ops.read_storyboard(unit) == before
    invalid = ops.preview_storyboard_changes(unit, b['revision'], [
        {'op': 'set_timeline_segments', 'shot_id': shot, 'segments': [[0, 1700]]}])
    assert not invalid['valid'] and invalid['preview'] is None
    assert '必须落在已有关键帧上' in invalid['conflicts'][0]['message']


def test_tool_schema_exposes_timeline_and_handoff_ops(board):
    from narrative import tool_api
    from narrative.project_package import layout

    ops, unit, b = board
    entry = layout(ops.project_info().path).entry
    first, second = b['shots'][0]['id'], b['shots'][1]['id']
    written = tool_api.call('apply_storyboard_changes', {'unit_id': unit, 'revision': b['revision'], 'changes': [
        {'op': 'add_timeline_keyframe', 'shot_id': first, 'offset_ms': 500, 'moment_name': '抬手'},
        {'op': 'set_timeline_segments', 'shot_id': first, 'segments': [[0, 500]]},
        {'op': 'set_shot_handoff', 'shot_id': second, 'from_shot': first, 'ref_frame': 'last'},
        {'op': 'set_asset_needs', 'needs': [{'kind': 'prop', 'name': '伞', 'variant': '折叠'}]}]}, project=entry)
    assert plan(written['shots'][0]['timeline'])[-1] == {'offset_ms': 500, 'moment_name': '抬手', 'description': ''}
    assert written['shots'][1]['handoff'] == {'from_shot': first, 'ref_frame': 'last'}
    assert written['asset_needs'] == [{'kind': 'prop', 'name': '伞', 'variant': '折叠'}]


def test_timeline_and_handoff_require_unapproved_scene(board, tmp_path):
    ops, unit, b = board
    shot = b['shots'][0]['id']
    image = tmp_path / 'ok.png'
    Image.new('RGB', (16, 9), 'gray').save(image)
    frames = [{'op': 'set_keyframe', 'shot_id': row['id'], 'offset_ms': 0, 'sketch_path': str(image)} for row in b['shots']]
    framed = apply(ops, unit, frames)
    approved = ops.apply_storyboard_changes(unit, framed['revision'], [{'op': 'set_approved', 'approved': True}])
    for change in ({'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 100},
                   {'op': 'set_shot_handoff', 'shot_id': shot, 'from_shot': b['shots'][1]['id']}):
        with pytest.raises(ValueError, match='解除定案'):
            ops.apply_storyboard_changes(unit, approved['revision'], [change])
