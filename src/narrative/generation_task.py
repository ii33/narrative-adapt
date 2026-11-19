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

KINDS = ('sketch', 'style', 'video')
# 绑定目标由 kind 决定，调用方不能改：生成类命令是即时动作，不接受「换个目标再写」。
BIND_TO = {'sketch': 'keyframe_sketch', 'style': 'keyframe_style', 'video': 'shot_generations'}
RECEIPT_FIELDS = {'sketch': ['sketch_path', 'sketch_source', 'sketch_basis_hash'],
                  'style': ['style_path'],
                  'video': ['generations']}
CONSTRAINT_FIELDS = ('aspect', 'resolution', 'duration_ms', 'first_frame', 'last_frame', 'source_image')
DEFAULT_ASPECT = '16:9'
# 构图参考图的尺寸沿用 storyboard_art 的画布：两处给模型的输入形状一致。
GUIDE_SIZE = (960, 540)
_SHADES = {'person': 150, 'prop': 168, 'location': 190, 'material': 176}


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


def build_task(kind, *, shot_id, offset_ms, board, layout=None, moment=None, assets=None,
               style=None, constraints=None, expect=None, prompt=''):
    """组装任务包；只读 board，不联网、不落盘。

    ``prompt`` 是本次的补充要求（可为空）。任务包里没有正文字段：正文、创作约定与
    资产说明由调用方通过 ``style``/``assets`` 带进来，这里不猜。

    只有 video 带 ``motion``：视频是「首帧→尾帧」的一段过渡，运动是它的输入之一；
    草图与风格帧是静态图，带上运镜只会让模型把运动画进这一帧里。
    """
    if kind not in KINDS:
        raise TaskInputError('生成类型须为' + '、'.join(KINDS))
    if not isinstance(board, dict) or not isinstance(board.get('shots'), list):
        raise TaskInputError('任务包需要该场的分镜数据')
    shot = next((row for row in board['shots'] if row.get('id') == shot_id), None)
    if shot is None:
        raise TaskInputError('指定的镜头不属于本场')
    offset = _offset(offset_ms)
    if offset not in _frame_offsets(shot):
        raise TaskInputError('该时刻还没有关键帧；先用 set_keyframe 建帧或 add_timeline_keyframe 建立计划')
    if not isinstance(prompt, str):
        raise TaskInputError('本次补充要求须为文本')
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
    """把资产卡摊成任务包的 assets：两个图字段合成一条图片序列，按顺序去重。

    调用方（Operations）拿的是 `_storyboard_sketch_basis` 里的资产卡；形状转换留在
    本模块，界面以后要单独组任务包时也能照用。
    """
    rows = []
    for card in cards or []:
        images = []
        for field in ('images', 'reference_images'):
            for image in card.get(field) or []:
                path = str(image.get('path') or '')
                if path and path not in images:
                    images.append(path)
        rows.append({'asset_id': card['id'], 'role': 'reference', 'images': images})
    return rows


def task_prompt(task):
    """把任务包摊成一段说明：只陈述包里已有的东西，不另外补剧情。"""
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
      文档里的三条：FLUX 3 Klein 素描、Krea 2 风格化、MiniMax H3 首尾帧）。开箱就能跑，
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
        # 内置生成技能里有一条（Krea 2 风格化）还没有随程序发布的工作流文件。缺声明本身
        # 不是错误——很多用户技能也走 provider 默认流程——但**内置**这一条缺了要说出来：
        # 否则用户看到「内置技能跑通了」，会以为它用的是文档里那个工作流。
        # 只对 sketch/style 报：视频走 HTTP 服务，本来就不读工作流文件（见下面那条分支）。
        if builtin and kind in ('sketch', 'style'):
            warnings.append({'code': 'WORKFLOW_FILE_ABSENT',
                             'message': '这条内置生成技能没有随程序发布工作流文件，本轮用 provider 的默认工作流；'
                                        '想指定文件请在技能里写 workflow.workflow_file',
                             'kind': kind})
        yield
        return
    variable = {'sketch': 'NARRATIVE_COMFY_SKETCH_WORKFLOW', 'style': 'NARRATIVE_COMFY_WORKFLOW'}.get(kind)
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
