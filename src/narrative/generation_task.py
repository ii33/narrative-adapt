"""生成任务包与执行器：把「这一次要生成什么」写成可核对的数据，再交给现有 provider。

为什么单独一层：技能声明了 kind/model/workflow（见 `skills.Skill.workflow`），界面与
助手需要的却是同一份「这次生成的输入」。把三件事拆开——

* ``build_task``：组装任务包（纯数据，不联网、不读工程文件）；
* ``select_skill``：按技能声明选一条——用户启用的同 kind 技能优先，没有就回落到内置
  生成技能（回执里标 ``skill.builtin=true``）；两边都没有才报错，不猜第三个默认；
* ``execute``：按 kind 调现有 provider，产出回执。

——失败时就能说清是「技能缺失」「输入不合法」还是「服务失败」：前两类在 build/select
阶段抛出，后一类包成 ``TaskProviderError``。执行失败不产生任何绑定，旧图保留。

本阶段刻意不重写工作流系统：``workflow.workflow_file`` 只通过 provider 现成的环境变量
临时生效，``workflow.params`` 没有通道，因此记进回执 warnings 而不是假装用上了。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

from .segment_motion import segment_at, segment_prompt
from .storyboard_layout import layout_at, normalize_layout

KINDS = ('sketch', 'style', 'video', 'asset_design', 'asset_sketch')
# 镜头级任务：分镜那条链路只认这三种；资产两级产物流水线走资产工具，
# 不能拿「镜头不属于本场」去回一个资产请求，那是误导。
SHOT_KINDS = ('sketch', 'style', 'video')
# 分镜的三种生成要镜头与时刻；资产的两类不要镜头，输入是资产卡上的文字稿与设定图。
# 分开列一份，是为了让「要镜头」与「要资产卡」两条校验各自说自己的话，而不是靠在
# 同一个函数里到处 if。
ASSET_KINDS = ('asset_design', 'asset_sketch')
# 绑定目标由 kind 决定，调用方不能改：生成类命令是即时动作，不接受「换个目标再写」。
BIND_TO = {'sketch': 'keyframe_sketch', 'style': 'keyframe_style', 'video': 'shot_generations',
           'asset_design': 'asset_designs', 'asset_sketch': 'asset_sketches'}
RECEIPT_FIELDS = {'sketch': ['sketch_path', 'sketch_source', 'sketch_basis_hash'],
                  'style': ['style_path'],
                  'video': ['generations'],
                  'asset_design': ['designs'],
                  'asset_sketch': ['sketches']}
CONSTRAINT_FIELDS = ('aspect', 'resolution', 'duration_ms', 'first_frame', 'last_frame', 'source_image')
# 资产没有镜头时长：图像 provider 只认输入图画布，所以可覆盖的约束只剩画幅。
ASSET_CONSTRAINT_FIELDS = ('aspect',)
DEFAULT_ASPECT = '16:9'
# 资产的默认画幅：人物与道具要看清比例与轮廓，地点要看清空间关系，所以不是同一张比例。
ASSET_ASPECTS = {'appearance': '2:3', 'item': '1:1', 'location': '16:9', 'material': '1:1'}
# 设定图的默认视角：人物与道具要正面看比例，场景没有「正面」这回事。
ASSET_DEFAULT_VIEWS = {'appearance': 'front', 'item': 'front', 'location': 'none', 'material': 'none'}
DESIGN_VIEWS = ('front', 'side', 'back', 'none')
# 构图参考图的尺寸沿用 storyboard_art 的画布：两处给模型的输入形状一致。
GUIDE_SIZE = (960, 540)
# 资产设定图的空白画布：图像工作流需要一张输入图，给空白画布而不是伪造构图
# （与既有 `generate_asset_sketch` 同一口径：没有构图可保，就不假装有）。
ASSET_CANVAS = {'2:3': (1024, 1536), '1:1': (1024, 1024), '16:9': (1536, 864)}
_SHADES = {'person': 150, 'prop': 168, 'location': 190, 'material': 176}
# 文字稿段落在提示词里的中文标签：给模型的说明要能直接对上写稿时填的键。
_DRAFT_LABELS = {'appearance': '外观', 'costume': '服装', 'prop': '道具形制',
                 'location': '场景要素', 'proportion': '比例', 'marks': '标志性细节'}



class TaskInputError(ValueError):
    """任务包或技能不成立（缺技能、参数不合法、kind 与技能不符）。"""


class TaskProviderError(ValueError):
    """provider 调用失败或产物无效（服务失败，不是内容问题）。"""


# ------------------------------------------------------------------ 任务包
def _offset(value):
    if type(value) is not int or value < 0:
        raise TaskInputError('offset_ms须为大于等于0的整数毫秒')
    return value


def _frame_offsets(shot):
    """该镜已存在的关键帧时刻：显式计划与静帧都算（读视图把两者并进时间轴）。"""
    rows = {row['offset_ms'] for row in (shot.get('timeline') or {}).get('keyframes') or []}
    rows |= {frame['offset_ms'] for frame in shot.get('keyframes') or []}
    return rows


def _moment(shot, offset_ms, moment):
    row = next((item for item in (shot.get('timeline') or {}).get('keyframes') or []
                if item['offset_ms'] == offset_ms), None) or {}
    result = {'name': row.get('moment_name', ''), 'description': row.get('description', '')}
    if moment is None:
        return result
    if not isinstance(moment, dict) or set(moment) - {'name', 'description'}:
        raise TaskInputError('moment只接受name与description两项文本')
    for key in ('name', 'description'):
        value = moment.get(key, result[key])
        if not isinstance(value, str):
            raise TaskInputError('moment的name与description须为文本')
        result[key] = value
    return result


def _assets(assets, shot, board):
    """参考资产：asset_id＋role＋images。

    role 只两种：``reference``（资产卡外观参考）与 ``element``（镜头视觉元素引用）；
    images 是实际会发给 provider 的参考图路径，由调用方从资产卡填。
    """
    if assets is None:
        rows = []
        for element in shot.get('visual_elements') or []:
            identifier = element.get('asset_id')
            if identifier and not any(item['asset_id'] == identifier for item in rows):
                rows.append({'asset_id': identifier, 'role': 'element', 'images': []})
        for obj in board.get('objects') or []:
            identifier = obj.get('asset_id')
            if identifier and not any(item['asset_id'] == identifier for item in rows):
                rows.append({'asset_id': identifier, 'role': 'reference', 'images': []})
        return rows
    if not isinstance(assets, list):
        raise TaskInputError('assets须为[{asset_id, role, images}]列表')
    rows, seen = [], set()
    for index, row in enumerate(assets):
        if not isinstance(row, dict) or set(row) - {'asset_id', 'role', 'images'}:
            raise TaskInputError(f'assets第{index + 1}项须含asset_id、role，可选images')
        identifier, role = row.get('asset_id'), row.get('role', 'reference')
        if not isinstance(identifier, str) or not identifier.strip():
            raise TaskInputError(f'assets第{index + 1}项需要非空asset_id')
        if role not in ('reference', 'element'):
            raise TaskInputError('assets的role须为reference或element')
        images = row.get('images') or []
        if not isinstance(images, list) or any(not isinstance(item, str) or not item.strip() for item in images):
            raise TaskInputError('assets的images须为非空路径数组')
        if identifier in seen:
            continue
        seen.add(identifier)
        rows.append({'asset_id': identifier, 'role': role, 'images': [str(Path(item)) for item in images]})
    return rows


def _style(style):
    if style is None:
        return {'text': '', 'references': []}
    if not isinstance(style, dict) or set(style) - {'text', 'references'}:
        raise TaskInputError('style只接受text与references')
    text = style.get('text', '')
    if not isinstance(text, str):
        raise TaskInputError('style的text须为文本')
    references = style.get('references') or []
    if not isinstance(references, list) or any(not isinstance(item, str) or not item.strip() for item in references):
        raise TaskInputError('style的references须为非空路径数组')
    return {'text': text, 'references': [str(Path(item)) for item in references]}


def _constraints(kind, shot, constraints, offset_ms):
    """输出约束。

    ``source_image`` 是 style 的输入草图、``first_frame``/``last_frame`` 是 video 的
    首尾帧：这三样只有调用方读得到工程文件，但它们是「这次生成的输入」而不是 provider
    自己的事，所以留在任务包里，执行器照着用。
    """
    aspect = next((shot[field].strip() for field in ('aspect_ratio', 'frame_aspect')
                   if isinstance(shot.get(field), str) and shot[field].strip()), DEFAULT_ASPECT)
    duration = shot.get('duration_ms')
    if type(duration) is not int or duration < 1:
        raise TaskInputError('镜头时长无效，无法作为生成时长约束')
    result = {'aspect': aspect, 'resolution': None, 'duration_ms': duration,
              'first_frame': None, 'last_frame': None, 'source_image': None}
    frames = shot.get('keyframes') or []
    if kind == 'style':
        frame = next((row for row in frames if row['offset_ms'] == offset_ms), None)
        source = (frame or {}).get('sketch_path')
        if not source:
            raise TaskInputError('风格帧需要该时刻已绑定的草图；先 set_keyframe 绑定草图再生成')
        result['source_image'] = source
    if kind == 'video':
        styled = [row for row in frames if row.get('style_path')]
        if styled:
            result['first_frame'] = styled[0]['style_path']
            result['last_frame'] = styled[-1]['style_path'] if len(styled) > 1 else None
    if constraints is None:
        return result
    if not isinstance(constraints, dict):
        raise TaskInputError('constraints须为对象')
    extra = set(constraints) - set(CONSTRAINT_FIELDS)
    if extra:
        raise TaskInputError('constraints有不认识的字段：' + '、'.join(sorted(extra))
                             + '；允许：' + '、'.join(CONSTRAINT_FIELDS))
    for key, value in constraints.items():
        if key == 'duration_ms':
            if type(value) is not int or value < 1:
                raise TaskInputError('constraints的duration_ms须为大于0的整数毫秒')
            result[key] = value
            continue
        if value is None:
            result[key] = None
            continue
        if not isinstance(value, str) or not value.strip():
            raise TaskInputError(f'constraints的{key}须为非空文本')
        result[key] = value.strip() if key == 'aspect' else str(Path(value))
    return result


def _expect(kind, expect):
    result = {'bind_to': BIND_TO[kind], 'receipt_fields': list(RECEIPT_FIELDS[kind])}
    if expect is None:
        return result
    if not isinstance(expect, dict) or set(expect) - {'bind_to', 'receipt_fields'}:
        raise TaskInputError('expect只接受bind_to与receipt_fields')
    if 'bind_to' in expect and expect['bind_to'] != result['bind_to']:
        raise TaskInputError(f'expect.bind_to固定为{result["bind_to"]}，不能改绑定目标')
    if 'receipt_fields' in expect:
        rows = expect['receipt_fields']
        if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows) \
                or set(rows) != set(result['receipt_fields']):
            raise TaskInputError('expect.receipt_fields须为' + '、'.join(result['receipt_fields']))
    return result


def _asset_draft(value):
    """文字稿进任务包的部分：段落与修订号。

    考据（``research``）与版本记录（``style_ref``）留在资产卡上：提示词要的是「画成什么
    样」，引文与版本是给人核对依据的，塞进提示词只会让模型去引用原文。
    """
    if not isinstance(value, dict):
        return None
    sections = {key: text.strip() for key, text in (value.get('sections') or {}).items()
                if isinstance(text, str) and text.strip()}
    if not sections:
        return None
    revision = value.get('revision')
    return {'sections': sections, 'revision': revision if type(revision) is int else 0}


def _asset_design(card, design_path):
    """素描图依据的设定图：指定了就按指定的，否则优先正面，再否则取最近一张。

    为什么不是「第一张」：设定图会越出越多（正面/侧面/背面/氛围），而跨镜一致最需要的
    是正面那张的形体；按列表首项取，等于让第一次生成的视角意外决定之后所有线稿。
    """
    designs = [row for row in card.get('designs') or []
               if isinstance(row, dict) and row.get('path')]
    if not designs:
        return None
    if design_path is not None:
        from pathlib import Path

        wanted = Path(str(design_path))
        chosen = next((row for row in designs if Path(row['path']) == wanted), None)
        if chosen is None:
            raise TaskInputError('指定的设定图不属于这张资产：' + str(design_path))
        return chosen
    return next((row for row in reversed(designs) if row.get('view') == 'front'), designs[-1])


def _asset_view(kind, category, view, design):
    """这次出的图是什么视角：显式给的优先；素描图沿用所依据设定图的视角。"""
    if view is not None:
        if not isinstance(view, str) or view not in DESIGN_VIEWS:
            raise TaskInputError('view须为' + '、'.join(DESIGN_VIEWS) + '或省略')
        return view
    if kind == 'asset_sketch' and design is not None:
        return design.get('view') if design.get('view') in DESIGN_VIEWS else 'none'
    return ASSET_DEFAULT_VIEWS[category]


def _asset_constraints(kind, category, design, constraints):
    """资产出图只约束画幅：图像 provider 只认输入图的尺寸，分辨率/时长没有通道。

    为什么只留 ``aspect`` 一项：两个 provider 都是按输入图画布出图的，写进来的分辨率会被
    默默忽略——列一项根本用不上的约束，比不列更糟。素描图的 ``source_image`` 是所依据的
    设定图：它是这次生成的输入，不是 provider 自己的事，所以和分镜的 ``source_image``
    一样留在任务包里（此时真正的画幅由输入图决定，``aspect`` 只是给人看的一致约定）。
    """
    result = {'aspect': ASSET_ASPECTS[category], 'source_image': None}
    if kind == 'asset_sketch':
        result['source_image'] = str(design['path'])
    if constraints is None:
        return result
    if not isinstance(constraints, dict):
        raise TaskInputError('constraints须为对象')
    extra = set(constraints) - set(ASSET_CONSTRAINT_FIELDS)
    if extra:
        raise TaskInputError('资产出图的constraints有不认识的字段：'
                             + '、'.join(sorted(extra)) + '；允许：' + '、'.join(ASSET_CONSTRAINT_FIELDS))
    for key, value in constraints.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise TaskInputError(f'constraints的{key}须为非空文本')
        if key == 'aspect' and value.strip() not in ASSET_CANVAS:
            raise TaskInputError('aspect须为' + '、'.join(ASSET_CANVAS) + f'，实际是{value.strip()}')
        result[key] = value.strip()
    return result


def _asset_task(kind, *, asset, asset_id=None, style=None, prompt='', constraints=None,
                expect=None, view=None, design_path=None, sketch_style=''):
    """资产类任务包：输入是资产卡上的文字稿、项目风格与（素描图才有的）设定图。

    不设强制闸门：没有文字稿也照样出设定图——软件不拦，缺什么在回执里说清楚（
    ``DRAFT_MISSING``）。只有素描图必须以一张设定图为依据，缺了就在这里报错，不去
    调服务浪费一次生成。
    """
    if not isinstance(asset, dict):
        raise TaskInputError('资产任务需要资产卡数据（asset）')
    card_id = asset.get('id')
    if not isinstance(card_id, str) or not card_id.strip():
        raise TaskInputError('资产卡缺少资产编号')
    if asset_id is not None and asset_id != card_id:
        raise TaskInputError('asset_id与资产卡不一致：' + str(asset_id))
    category = asset.get('category')
    if category not in ASSET_ASPECTS:
        raise TaskInputError('资产类别须为' + '、'.join(ASSET_ASPECTS) + '，无法确定画幅')
    if not isinstance(sketch_style, str):
        raise TaskInputError('素描画法须为文本')
    draft = _asset_draft(asset.get('draft'))
    design = None
    if kind == 'asset_sketch':
        design = _asset_design(asset, design_path)
        if design is None:
            raise TaskInputError('这张资产还没有设定图：asset_sketch必须以一张设定图为依据，'
                                 '请先生成设定图（或用design_path指定已有的那张）')
    task = {'kind': kind, 'asset_id': card_id, 'title': str(asset.get('title') or ''),
            'category': category, 'draft': draft, 'design': design,
            'view': _asset_view(kind, category, view, design),
            'style': _style(style),
            'constraints': _asset_constraints(kind, category, design, constraints),
            'expect': _expect(kind, expect), 'prompt': prompt}
    if kind == 'asset_sketch':
        # 与 video 的 motion 同一处理：只有这条流程有的输入才出现在任务包里，
        # 免得读包的人以为设定图那种单张静态图也要一段「素描画法」。
        task['sketch_style'] = sketch_style
    return task


def _motion_prompt(motion, prompt, *, duration_ms=None, pictures=2):
    """视频提示词：官方对齐句＋四段推导在前，人工补充在后并声明优先。

    为什么人工放最后而不是让它盖掉机器段：机器段是从前后两帧框图量出来的差量（谁往哪走、
    变大还是变小），人工写的是意图；冲突时以人为准，但不能因此把量出来的事实从提示词里删掉
    ——否则复盘时说不清「这一版到底交代了什么」。所以两段都留，位置说明优先级。

    ``duration_ms`` 是这次请求要生成的时长：官方要求对齐句里的 ``S.SS`` 与请求时长一致，
    所以用 ``constraints.duration_ms``（不是镜长）来写；``pictures`` 决定用 FL2VA 还是 I2VA
    那句对齐句——只有一张风格帧时不能凭空写 Picture 2。
    """
    machine = segment_prompt(motion, 'video', duration_ms=duration_ms, pictures=pictures)
    if not prompt.strip():
        return machine
    return machine + '\n人工补充（与上面的程序推导冲突时以这里为准）：' + prompt.strip()


def build_task(kind, *, shot_id=None, offset_ms=None, board=None, layout=None, moment=None, assets=None,
               style=None, constraints=None, expect=None, prompt='', asset=None, asset_id=None,
               view=None, design_path=None, sketch_style=''):
    """组装任务包；只读传入的数据，不联网、不落盘。

    ``prompt`` 是本次的补充要求（可为空）。任务包里没有正文字段：正文、创作约定与
    资产说明由调用方通过 ``style``/``assets`` 带进来，这里不猜。

    分镜的三种生成要镜头（``shot_id``/``offset_ms``/``board``）；资产的两类要 ``asset``
    卡（文字稿与设定图都在卡上，见 `_asset_task`）——两条路的校验各说各的，不在一个
    函数里互相迁就。

    只有 video 带 ``motion``：视频是「首帧→尾帧」的一段过渡，运动是它的输入之一；
    草图与风格帧是静态图，带上运镜只会让模型把运动画进这一帧里。
    """
    if kind not in KINDS:
        raise TaskInputError('生成类型须为' + '、'.join(KINDS))
    if not isinstance(prompt, str):
        raise TaskInputError('本次补充要求须为文本')
    if kind in ASSET_KINDS:
        return _asset_task(kind, asset=asset, asset_id=asset_id, style=style, prompt=prompt,
                           constraints=constraints, expect=expect, view=view,
                           design_path=design_path, sketch_style=sketch_style)
    if not isinstance(board, dict) or not isinstance(board.get('shots'), list):
        raise TaskInputError('任务包需要该场的分镜数据')
    shot = next((row for row in board['shots'] if row.get('id') == shot_id), None)
    if shot is None:
        raise TaskInputError('指定的镜头不属于本场')
    offset = _offset(offset_ms)
    if offset not in _frame_offsets(shot):
        raise TaskInputError('该时刻还没有关键帧；先用 set_keyframe 建帧或 add_timeline_keyframe 建立计划')
    box = normalize_layout(layout, shot_id=shot['id'], offset_ms=offset) if layout is not None else layout_at(shot, offset)
    task = {'kind': kind, 'shot_id': shot['id'], 'offset_ms': offset, 'layout': box,
            'moment': _moment(shot, offset, moment), 'assets': _assets(assets, shot, board),
            'style': _style(style), 'constraints': _constraints(kind, shot, constraints, offset),
            'expect': _expect(kind, expect), 'prompt': prompt}
    if kind == 'video':
        # 段按 offset_ms 定位：视频的 offset 通常就是段起点；推导是纯函数，这里重算一次
        # 就不会依赖调用方是否先过一遍读视图。
        task['motion'] = segment_at(board, shot, offset)
        task['prompt'] = _motion_prompt(task['motion'], prompt,
                                        duration_ms=task['constraints']['duration_ms'],
                                        pictures=2 if task['constraints'].get('last_frame') else 1)
    return task


def assets_from_cards(cards):
    """把资产卡摊成任务包的 assets：几张图合成一条图片序列，按顺序去重。

    设定图与素描图也算资产图：它们本来就是「这张资产长什么样」的依据，分镜出图时最该
    拿到的就是这几张。调用方（Operations）拿的是 `_storyboard_sketch_basis` 里的资产卡；
    形状转换留在本模块，界面以后要单独组任务包时也能照用。
    """
    rows = []
    for card in cards or []:
        images = []
        for field in ('images', 'reference_images', 'designs', 'sketches'):
            for image in card.get(field) or []:
                path = str(image.get('path') or '')
                if path and path not in images:
                    images.append(path)
        rows.append({'asset_id': card['id'], 'role': 'reference', 'images': images})
    return rows


def _asset_prompt(task):
    """资产出图的说明：文字稿逐段在前，风格与画幅在后，最后才是本次补充要求。

    为什么文字稿逐段摆出来而不是揉成一段：出图的人（和复核的人）要能一眼看出「这一版
    画的是哪一段」，也才说得清某张设定图漏了哪条标志性细节。
    """
    draft = task.get('draft')
    design = task.get('design')
    lines = ['资产：' + '／'.join(filter(None, [task.get('category', ''), task.get('title', '')]))]
    if draft:
        lines.append(f'文字稿（第{draft["revision"]}版）：')
        lines.extend(_DRAFT_LABELS.get(key, key) + '：' + text for key, text in draft['sections'].items())
    else:
        lines.append('文字稿：无。这次没有文字依据，跨镜一致性没有依据可核；'
                     '先写文字稿再出图，图与图之间才有可比的标准。')
    if design:
        lines.append('设定图依据（' + str(design.get('view') or 'none') + '视角；生成时文字稿第'
                     + str(design.get('draft_revision') or 0) + '版）：本图只按这张设定图定形体')
    if task['style']['text']:
        lines.append('项目风格与创作约定：' + task['style']['text'])
    output = {key: value for key, value in task['constraints'].items()
              if value is not None and key != 'source_image'}
    if output:
        lines.append('输出约束：' + json.dumps(output, ensure_ascii=False))
    if task['kind'] == 'asset_design':
        lines.append('画这张资产的设定图：单一视角、中性背景、无文字标注，不加入任何情节点；'
                     '形体、比例与标志性细节以文字稿为准。需要别的视角就按 view 再出一张，'
                     '不要把多个视角挤进同一帧。')
    else:
        if task.get('sketch_style'):
            lines.append('素描画法：' + task['sketch_style'])
        lines.append('按设定图画工作用线稿：比例、轮廓、朝向与标志性细节都与设定图一致；'
                     '线条为主、留白、不画成插图，不画文字或界面元素。')
    lines.append('参考图只作外观依据，不把参考图里的文字、标签或排版画进成图。')
    if task['prompt']:
        lines.append('本次补充要求：' + task['prompt'])
    return '\n'.join(lines)


def task_prompt(task):
    """把任务包摊成一段说明：只陈述包里已有的东西，不另外补剧情。"""
    if task.get('kind') in ASSET_KINDS:
        return _asset_prompt(task)
    lines = []
    moment = task['moment']
    if moment.get('name') or moment.get('description'):
        lines.append('时刻：' + '／'.join(filter(None, [moment['name'], moment['description']])))
    box = task['layout']
    if box:
        lines.append('机位（x/y/z米、yaw/pitch度、等效焦距与景别）：' + json.dumps(box['camera'], ensure_ascii=False))
        if box['objects']:
            lines.append('画面主体（bbox为归一化画面坐标[x0,y0,x1,y1]，原点左上，depth为景深米）：'
                         + json.dumps(box['objects'], ensure_ascii=False))
        if box['unmet_needs']:
            lines.append('本帧仍缺的资产：' + json.dumps(box['unmet_needs'], ensure_ascii=False))
    if task['assets']:
        lines.append('参考资产：' + json.dumps([{'asset_id': row['asset_id'], 'role': row['role']}
                                            for row in task['assets']], ensure_ascii=False))
    if task['style']['text']:
        lines.append('项目风格与创作约定：' + task['style']['text'])
    output = {key: value for key, value in task['constraints'].items()
              if value is not None and key != 'source_image'}
    if output:
        lines.append('输出约束：' + json.dumps(output, ensure_ascii=False))
    lines.append({'sketch': '按正文与视觉元素画这一帧的描述型素描；只绘制当前时刻，后续时刻不得提前显露。',
                  'style': '把输入草图转成最终画面：保留视点、构图、轮廓、调度与主光方向。',
                  'video': '按首尾帧生成一段连贯动画；遵循动作因果、揭露顺序与停顿，不为填满时长添加独立剧情。'
                  }[task['kind']])
    lines.append('参考图只作外观与构图依据，不把参考图的文字、标签或运动箭头画进成图。')
    if task['prompt']:
        # video 的 prompt 已经是「四段机器推导＋人工补充」：换个标题，别把它标成「补充要求」，
        # 否则读回执的人会以为运动是这次临时加的，而它其实是这一段本身的差量。
        lines.append(('运动衔接（首尾帧之间这一段）：' if 'motion' in task else '本次补充要求：')
                     + task['prompt'])
    return '\n'.join(lines)


# ------------------------------------------------------------------ 技能
def select_skill(kind, skills=None):
    """在「已启用且 workflow.kind 匹配」的技能里选一条：用户技能优先，没有就回落到内置。

    优先级只有两层，理由各不相同：

    * 用户技能内部取**最近更新**的一条——他改完一条就期望生效（与 `prompt_registry`
      的 `replaces` 同一口径）。
    * 用户一条都没有（或都停用了）时用内置生成技能（`skills.BUILTIN_GENERATION_SKILLS`，
      文档里的三条：FLUX 3 Klein 素描、Krea 2 风格化、MiniMax H3 首尾帧，加上资产的两条：
      资产设定图、资产素描图）。开箱就能跑，
      而不是让人先猜该建什么技能。内置是「默认」不是「最高优先」：只要用户声明了同 kind
      的启用技能，一律以用户的为准，不拿内置去比时间戳（内置没有时间戳，比出来只会是
      用户的、但那样规则就变成「依赖时间戳顺序」这种说不清的东西了）。

    两边都找不到才报错并列出当前启用的生成技能：这条链路不猜第三个默认，因为不同技能
    对应不同模型与许可（付费/本地），猜错就是替用户花钱或换模型。
    """
    if kind not in KINDS:
        raise TaskInputError('生成类型须为' + '、'.join(KINDS))
    if skills is None:
        from .skills import load_skills
        skills = load_skills()
    declared = [row for row in skills if isinstance(getattr(row, 'workflow', None), dict) and row.workflow]
    enabled = [row for row in declared if getattr(row, 'enabled', True)]
    matches = [row for row in enabled if row.workflow.get('kind') == kind]
    custom = [row for row in matches if not getattr(row, 'builtin', False)]
    if custom:
        return max(custom, key=lambda row: (getattr(row, 'updated_at', '') or '',
                                            getattr(row, 'created_at', '') or ''))
    if matches:
        # 只剩内置：同一 kind 目前只声明了一条，取声明顺序里的第一条（`max` 在这里
        # 没有意义：内置没有时间戳，比出来的先后只是列表顺序，明说更不容易被误读）。
        return matches[0]
    available = '、'.join(f'{row.name}（{row.workflow.get("kind")}）' for row in enabled) or '无'
    raise TaskInputError(f'没有可用于{kind}生成的技能：请在技能里声明 workflow.kind={kind} 并启用；'
                         f'当前启用的生成技能：{available}')


# ------------------------------------------------------------------ 执行
def _stamp():
    return datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


@contextmanager
def _workflow_override(kind, workflow, warnings, *, builtin=False):
    """把技能里的 workflow_file 透给现有 provider。

    现有 provider 只认环境变量（``NARRATIVE_COMFY_SKETCH_WORKFLOW`` /
    ``NARRATIVE_COMFY_WORKFLOW``），所以这里临时设置、退出还原——能生效就生效，
    不能（视频走 HTTP 服务、文件不存在、没有通道的 params）就记 warning，
    不假装用上了，也不在本阶段重写工作流系统。``builtin`` 只影响 warning 的措辞：
    内置技能缺文件是「程序没带」，用户技能缺文件是「你没写」。
    """
    params = workflow.get('params')
    if params:
        warnings.append({'code': 'WORKFLOW_PARAMS_UNSUPPORTED',
                         'message': '技能里的 workflow.params 暂时没有透传通道'
                                    '（现有 provider 只接受工作流文件与环境变量）；本轮未使用',
                         'params': deepcopy(params)})
    workflow_file = workflow.get('workflow_file')
    if not workflow_file:
        # 内置生成技能里有两条（Krea 2 风格化、资产设定图）还没有随程序发布的工作流文件。
        # 缺声明本身不是错误——很多用户技能也走 provider 默认流程——但**内置**这一条缺了
        # 要说出来：否则用户看到「内置技能跑通了」，会以为它用的是文档里那个工作流。
        # 只对读工作流文件的 kind 报：视频走 HTTP 服务，本来就不读（见下面那条分支）。
        if builtin and kind in ('sketch', 'style', 'asset_design', 'asset_sketch'):
            warnings.append({'code': 'WORKFLOW_FILE_ABSENT',
                             'message': '这条内置生成技能没有随程序发布工作流文件，本轮用 provider 的默认工作流；'
                                        '想指定文件请在技能里写 workflow.workflow_file',
                             'kind': kind})
        yield
        return
    # 资产两类与分镜共用同一批 provider：设定图走风格工作流、素描图走素描工作流，
    # 所以环境变量按「用哪个 provider」映射，而不是按业务名各起一套。
    variable = {'sketch': 'NARRATIVE_COMFY_SKETCH_WORKFLOW', 'asset_sketch': 'NARRATIVE_COMFY_SKETCH_WORKFLOW',
                'style': 'NARRATIVE_COMFY_WORKFLOW', 'asset_design': 'NARRATIVE_COMFY_WORKFLOW'}.get(kind)
    if variable is None:
        warnings.append({'code': 'WORKFLOW_FILE_UNUSED',
                         'message': '本阶段视频生成走 MiniMax H3 HTTP 服务，不读 ComfyUI 工作流文件；'
                                    '技能里的 workflow_file 本轮未使用',
                         'workflow_file': workflow_file})
        yield
        return
    path = Path(workflow_file)
    if not path.is_absolute():
        # 相对路径就是「随程序发布的工作流文件」：内置技能写的是
        # "workflows/storyboard_flux_klein.json"，按包目录解析而不是按进程 CWD——
        # 用户从哪个目录启动程序都该拿到同一份。自己写绝对路径的照原样用。
        path = Path(__file__).parent / path
    if not path.is_file():
        warnings.append({'code': 'WORKFLOW_FILE_MISSING',
                         'message': f'技能里的 workflow_file 不存在（{path.name}），本轮改用默认工作流',
                         'workflow_file': workflow_file})
        yield
        return
    previous = os.environ.get(variable)
    os.environ[variable] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


def _guide_image(layout):
    """把框图折成一张灰度构图参考图：只画主体框、地平线与人物头部位置。

    它只是发给模型的输入（和 storyboard_art 里那份 render_sketch 同一用途），
    不是交付图：最终图必须来自模型，几何绘制不能顶替生成。
    """
    width, height = GUIDE_SIZE
    image = Image.new('L', (width, height), 210)
    draw = ImageDraw.Draw(image)
    draw.line([(0, round(height * .62)), (width, round(height * .62))], fill=120, width=2)
    for row in sorted((layout or {}).get('objects') or [], key=lambda item: item['depth'], reverse=True):
        x0, y0, x1, y1 = (row['bbox'][0] * width, row['bbox'][1] * height,
                          row['bbox'][2] * width, row['bbox'][3] * height)
        shade = _SHADES[row['kind']]
        draw.rectangle((x0, y0, x1, y1), outline=60, fill=shade)
        if row['kind'] == 'person':
            head = max(2.0, (y1 - y0) * .22)
            draw.ellipse((x0, y0, x1, y0 + head), outline=60, fill=min(shade + 25, 235))
    return image


def _provider_call(callback, kind):
    """provider 的 ValueError/OSError 一律归为「服务失败」：它们不是内容校验问题。"""
    try:
        return callback()
    except (ValueError, OSError) as error:
        raise TaskProviderError(f'{kind}生成服务调用失败：{error}') from error


def _verify_image(path):
    try:
        with Image.open(path) as image:
            image.load()
            if image.width < 1 or image.height < 1:
                raise ValueError('生成结果宽或高为0')
    except (OSError, SyntaxError, ValueError) as error:
        raise TaskProviderError('生成服务没有返回可读图片：' + str(error)) from error


def _video_credentials(credentials):
    if not isinstance(credentials, dict):
        raise TaskInputError('视频生成需要本工程的视频服务配置与密钥')
    base_url, key_env = credentials.get('base_url'), credentials.get('key_env')
    key = credentials.get('api_key') or (os.environ.get(key_env, '') if isinstance(key_env, str) else '')
    if not key:
        raise TaskInputError('尚未配置视频生成密钥，请先打开视频服务设置')
    return base_url, key_env, key


def _submit_video(task, workflow, credentials, folder):
    """视频只接首尾帧（endpoints）：参考运动模式要现成草图动画，不在本轮接通范围。"""
    from .storyboard_video_provider import MODELS, build_request, submit

    constraints = task['constraints']
    images = [path for path in (constraints.get('first_frame'), constraints.get('last_frame')) if path]
    if not images:
        raise TaskInputError('本阶段视频生成只接首尾帧（endpoints）：请先为本镜生成风格帧；'
                             '参考运动模式不在本轮接通范围')
    model = workflow.get('model')
    if model not in MODELS:
        raise TaskInputError(f'视频技能声明的模型不可用（{model}）；可选：' + '、'.join(MODELS))
    request = build_request(model=model, resolution=constraints.get('resolution') or '768P',
                            duration_ms=constraints['duration_ms'], mode='endpoints',
                            prompt=task_prompt(task), images=images)
    base_url, key_env, key = _video_credentials(credentials)
    raw = json.dumps(request).encode()
    request_file = Path(folder) / 'reviewed-request.json'
    request_file.write_bytes(raw)
    result = _provider_call(lambda: submit(base_url, key, request), 'video')
    return {'provider': 'minimax-h3', 'request': request, 'request_sha256': hashlib.sha256(raw).hexdigest(),
            'request_path': str(request_file), 'response': result, 'base_url': base_url, 'key_env': key_env,
            'model': request['model'], 'resolution': request['resolution'],
            'task_id': result.get('task_id') if isinstance(result, dict) else None}


def execute(task, skill, *, work_dir=None, credentials=None):
    """按 kind 调现有 provider，返回回执。

    ``work_dir`` 是新图落地的目录：绑定要在写完之前完成（``_write_storyboard`` 会把
    文件复制进工程目录），所以由调用方给一个活着的临时目录；不给就自己建一个，
    由调用方负责清理。``credentials`` 只在 video 用：``{base_url, key_env, api_key}``，
    密钥只用于提交，不进回执。
    """
    kind = task.get('kind')
    if kind not in KINDS:
        raise TaskInputError('生成类型须为' + '、'.join(KINDS))
    workflow = getattr(skill, 'workflow', None)
    declared = workflow.get('kind') if isinstance(workflow, dict) else None
    if declared != kind:
        raise TaskInputError(f'技能{getattr(skill, "name", "")}声明的生成类型是'
                             f'{declared or "未声明"}，与本次要求的{kind}不一致')
    folder = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix=f'generation-task-{kind}-'))
    folder.mkdir(parents=True, exist_ok=True)
    started = _stamp()
    warnings = []
    if kind in ASSET_KINDS and not task.get('draft'):
        # 软件不拦（没有文字稿也能出图），但一致性确实没有依据：说出来，让人自己决定
        # 是先补文字稿，还是接受这一版只是「先看看」的探索稿。
        warnings.append({'code': 'DRAFT_MISSING',
                         'message': '这张资产还没有文字稿：图照常生成，但跨镜一致性没有文字依据可核；'
                                    '建议先写文字稿再出图',
                         'asset_id': task.get('asset_id')})
    output = folder / f'{kind}.png'
    references = tuple(task['style']['references'])
    builtin = bool(getattr(skill, 'builtin', False))
    with _workflow_override(kind, workflow, warnings, builtin=builtin):
        if kind == 'sketch':
            from .storyboard_style_provider import render_sketch_image
            guide = folder / 'guide.png'
            _guide_image(task['layout']).save(guide)
            receipt = _provider_call(
                lambda: render_sketch_image(guide, task_prompt(task), output, reference_images=references), kind)
        elif kind == 'style':
            from .storyboard_style_provider import render_style_image
            source = task['constraints'].get('source_image')
            receipt = _provider_call(
                lambda: render_style_image(Path(source), task_prompt(task), output, reference_images=references), kind)
        elif kind == 'asset_design':
            # 设定图没有构图可保：与既有资产素描同一个办法——给一张空白画布当输入，
            # 画幅按类别给，绝不拿几何图形冒充「设计稿」。
            from .storyboard_style_provider import render_style_image
            canvas = folder / 'asset-canvas.png'
            size = ASSET_CANVAS.get(task['constraints'].get('aspect'), (1024, 1024))
            Image.new('RGB', size, 'white').save(canvas)
            receipt = _provider_call(
                lambda: render_style_image(canvas, task_prompt(task), output, reference_images=references), kind)
        elif kind == 'asset_sketch':
            # 素描图的输入就是设定图本身：provider 走图像编辑路线，形体由它定，
            # 文字稿与素描画法只决定「画成线稿的哪一面、多简」。
            from .storyboard_style_provider import render_sketch_image
            source = task['constraints'].get('source_image')
            if not source:
                raise TaskInputError('asset_sketch需要一张设定图作为输入')
            receipt = _provider_call(
                lambda: render_sketch_image(Path(source), task_prompt(task), output, reference_images=references), kind)
        else:
            receipt = _submit_video(task, workflow, credentials, folder)
    if kind != 'video':
        _verify_image(output)
    return {'task': task,
            'skill': {'name': getattr(skill, 'name', ''), 'model': workflow.get('model', ''),
                      'license': workflow.get('license', ''),
                      'expectations': deepcopy(workflow.get('expectations') or {}),
                      # 内置 = 用户没启用同 kind 的技能，这段是用程序自带的默认生成技能跑的。
                      # 回执里必须标出来：界面与助手要能分清「用户配的」和「默认的」，
                      # 否则换个机器/换个人就说不清这条结果到底按谁的许可与模型出的。
                      'builtin': builtin},
            'provider_receipt': receipt, 'warnings': warnings,
            'started_at': started, 'finished_at': _stamp(),
            # 绑定要在临时目录被回收之前完成；视频没有本地新图，这里是 None。
            'output_path': None if kind == 'video' else str(output)}
