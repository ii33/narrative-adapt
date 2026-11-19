"""Render grayscale blocking sketches and export an approved shot for image-to-image work."""

from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

W, H = 960, 540


class SketchSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    kind: Literal["person", "box", "table", "door", "vehicle", "rock"] = "person"
    x: float = Field(default=0.48, ge=0, le=1)
    y: float = Field(default=0.86, ge=0, le=1)
    scale: float = Field(default=1, ge=0.15, le=2)
    pose: Literal["stand", "reach", "sit", "walk"] = "stand"
    facing: Literal["right", "left"] = "right"


class SketchLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    composition: Literal["single", "two_shot", "object", "empty"] = "single"
    framing: Literal["wide", "medium", "close"] = "medium"
    light: Literal["left", "right", "back", "overhead"] = "left"
    setting: Literal["interior", "exterior"] = "interior"
    subjects: list[SketchSubject] | None = Field(default=None, max_length=12)


def _subjects(plan):
    if plan.subjects is not None:
        return plan.subjects
    return {
        "single": [SketchSubject()],
        "two_shot": [
            SketchSubject(x=0.31),
            SketchSubject(x=0.7, y=0.82, scale=0.91, facing="left"),
        ],
        "object": [SketchSubject(kind="box", x=0.52, scale=1.2)],
        "empty": [],
    }[plan.composition]


def _backdrop(plan):
    horizon = 322
    base = Image.new("L", (W, H), 218)
    draw = ImageDraw.Draw(base)
    if plan.setting == "interior":
        draw.polygon([(0, 0), (155, 93), (155, horizon), (0, 470)], fill=174, outline=104)
        draw.polygon([(W, 0), (803, 93), (803, horizon), (W, 470)], fill=192, outline=108)
        draw.polygon([(155, 93), (803, 93), (803, horizon), (155, horizon)], fill=209, outline=112)
        draw.line([(0, 470), (155, horizon), (803, horizon), (W, 470)], fill=112, width=2)
        for x in range(-400, 1500, 200):
            draw.line([(480 + (x - 480) * 0.32, horizon + 2), (x, H)], fill=182)
    else:
        draw.polygon(
            [
                (0, 276),
                (97, 210),
                (231, 260),
                (352, 185),
                (480, 254),
                (621, 202),
                (770, 269),
                (888, 216),
                (W, 255),
                (W, H),
                (0, H),
            ],
            fill=169,
            outline=102,
        )
        draw.polygon(
            [(0, 331), (262, 301), (532, 318), (790, 289), (W, 310), (W, H), (0, H)], fill=194
        )
    light_center = {
        "left": (-0.05, 0.03),
        "right": (1.05, 0.03),
        "back": (0.50, 0.35),
        "overhead": (0.50, -0.25),
    }[plan.light]
    illumination = Image.new("L", (96, 54))
    illumination.putdata(
        [
            int(
                max(
                    0,
                    253
                    - 145
                    * ((x / 95 - light_center[0]) ** 2 + (y / 53 - light_center[1]) ** 2) ** 0.5,
                )
            )
            for y in range(54)
            for x in range(96)
        ]
    )
    return Image.blend(base, illumination.resize((W, H), Image.Resampling.BICUBIC), 0.37)


def _draw_subject(draw, subject, plan):
    k = {"wide": 0.67, "medium": 1.0, "close": 1.72}[plan.framing] * subject.scale
    x, y = W * subject.x, H * subject.y
    if plan.framing == "close":
        y += 126 * subject.scale
    direction = -1 if subject.facing == "left" else 1

    def point(a, b):
        return round(x + a * k * direction), round(y + b * k)

    def shape(coords, fill):
        points = [point(*pair) for pair in coords]
        draw.polygon(points, fill=fill)
        draw.line(points + [points[0]], fill=51, width=max(2, round(2 * k)), joint="curve")

    def ellipse(a, b, c, d, fill):
        left, top = point(a, b)
        right, bottom = point(c, d)
        draw.ellipse(
            (min(left, right), top, max(left, right), bottom),
            fill=fill,
            outline=52,
            width=max(2, round(2 * k)),
        )

    shade = 87 if plan.light == "back" else 155
    shadow = {"left": 110, "right": -110, "back": 25, "overhead": 8}[plan.light]
    draw.polygon(
        [
            (x - 27 * k, y - 4 * k),
            (x + 23 * k, y - 5 * k),
            (x + (shadow + 30) * k, y + 15 * k),
            (x + shadow * k, y + 18 * k),
        ],
        fill=112,
    )
    if subject.kind == "person":
        head_y = -148 if subject.pose == "sit" else -207
        hip = head_y + 124
        leg1, leg2 = {
            "stand": ((-16, -37), (16, -37)),
            "reach": ((-18, -38), (20, -38)),
            "walk": ((-30, -32), (30, -43)),
            "sit": ((42, -48), (13, -39)),
        }[subject.pose]
        for knee, foot, side in [(leg1, leg1[0] - 5, -11), (leg2, leg2[0] + 4, 11)]:
            shape(
                [
                    (side - 7, hip),
                    (side + 7, hip),
                    (knee[0] + 5, knee[1]),
                    (foot + 7, 0),
                    (foot - 4, 0),
                    (knee[0] - 5, knee[1]),
                ],
                shade,
            )
        shape(
            [
                (-17, head_y + 35),
                (14, head_y + 33),
                (23, head_y + 65),
                (14, hip),
                (-17, hip),
                (-25, head_y + 62),
            ],
            shade,
        )
        arm = [
            (11, head_y + 43),
            (21, head_y + 40),
            (43, head_y + 67),
            (77, head_y + 60),
            (80, head_y + 67),
            (41, head_y + 78),
            (19, head_y + 64),
        ]
        if subject.pose != "reach":
            arm = [
                (10, head_y + 43),
                (23, head_y + 43),
                (26, head_y + 88),
                (21, head_y + 112),
                (14, head_y + 109),
                (15, head_y + 85),
            ]
        shape(arm, min(shade + 20, 194))
        ellipse(-19, head_y - 8, 16, head_y + 31, min(shade + 27, 212))
        draw.line(
            [point(9, head_y + 2), point(17, head_y + 12), point(11, head_y + 14)], fill=49, width=2
        )
    elif subject.kind == "rock":
        shape([(-72, -15), (-46, -63), (3, -84), (49, -64), (69, -24), (52, -3), (-45, 0)], shade)
        shape([(3, -84), (49, -64), (69, -24), (27, -31)], min(shade + 39, 212))
    elif subject.kind == "vehicle":
        shape(
            [
                (-108, -15),
                (-103, -48),
                (-66, -54),
                (-34, -85),
                (43, -83),
                (67, -55),
                (107, -43),
                (108, -14),
            ],
            shade,
        )
        shape([(-55, -54), (-31, -77), (2, -77), (2, -54)], 206)
        for wheel in [-66, 64]:
            ellipse(wheel - 16, -31, wheel + 16, 0, 65)
    elif subject.kind == "door":
        shape([(-42, 0), (-42, -195), (42, -195), (42, 0)], shade)
        ellipse(24, -99, 30, -93, 211)
    else:
        height = 91 if subject.kind == "table" else 65
        shape(
            [(-62, -height), (-35, -height - 18), (72, -height - 18), (55, -height)],
            min(shade + 48, 222),
        )
        if subject.kind == "table":
            for leg in [-54, 43]:
                shape([(leg, -height), (leg + 8, -height), (leg + 8, 0), (leg, 0)], shade)
        else:
            shape([(-62, -height), (55, -height), (55, 0), (-62, 0)], shade)
            shape([(55, -height), (72, -height - 18), (72, -17), (55, 0)], shade - 25)


def render_sketch(spec: dict) -> Image.Image:
    plan = SketchLayout.model_validate(spec)
    image = _backdrop(plan)
    draw = ImageDraw.Draw(image)
    for subject in sorted(_subjects(plan), key=lambda s: s.y):
        _draw_subject(draw, subject, plan)
    return image


def render_camera_guide(layout, view, objects):
    """Approximate projected silhouettes; the image model supplies actual anatomy."""
    plan = SketchLayout.model_validate(layout)
    image = _backdrop(plan)
    draw = ImageDraw.Draw(image)
    by_id = {obj['id']: obj for obj in objects}
    for projection in sorted(view.get('projections', []), key=lambda row: row['depth'], reverse=True):
        if not projection['visible']:
            continue
        obj = by_id[projection['object_id']]
        x, y = projection['screen_x'] * W, projection['screen_y'] * H
        height = max(1, projection['size_ratio'] * H)
        left, right, top, bottom = x - height * .18, x + height * .18, y - height / 2, y + height / 2
        if obj['kind'] == 'person':
            draw.ellipse((x-height*.085, top, x+height*.085, top+height*.17), fill=155, outline=52, width=2)
            draw.polygon([(left, top+height*.2), (right, top+height*.2), (x+height*.1, bottom-height*.4),
                          (x-height*.1, bottom-height*.4)], fill=155, outline=52)
            draw.line((x-height*.05, bottom-height*.4, x-height*.08, bottom), fill=52, width=max(2, round(height*.035)))
            draw.line((x+height*.05, bottom-height*.4, x+height*.08, bottom), fill=52, width=max(2, round(height*.035)))
        else:
            draw.rectangle((left, top, right, bottom), fill=150, outline=52, width=2)
    return image


def _file_sha256(path, label):
    """按内容比，不按路径比：同一个文件换名字不算依据变化。"""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'{label}缺失，请补齐后再生成')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frame_diagram(shot, offset_ms):
    """该时刻的框图依据：框图哈希为主，已绑定草图文件的哈希另记一栏。

    `frame_id` 用「镜号@时刻」这种可读身份，而不是数据库分配的帧编号：框图可以先于
    草图存在（先排构图、后出图），那时还没有帧编号；可读身份两个阶段都稳定。
    两者都没有就返回 None——这一帧还没有任何可视依据。
    """
    from .storyboard_layout import layout_at, layout_hash

    layout = layout_at(shot, offset_ms)
    frame = next((row for row in shot.get('keyframes') or [] if row.get('offset_ms') == offset_ms), None)
    sketch = (frame or {}).get('sketch_path')
    if layout is None and not sketch:
        return None
    return {'frame_id': f"{shot['id']}@{offset_ms}",
            'sha256': layout_hash(layout) if layout is not None else _file_sha256(sketch, '生成依据框图'),
            'sketch_sha256': _file_sha256(sketch, '生成依据草图') if sketch else None}


def handoff_reference(board, shot):
    """衔接帧：把来源镜头的末帧（或首帧）当本镜首帧的参考图。

    来源镜还没出图时不算依据变化，只是这次没有参考图可发；来源图被换了，
    依据才真的变。
    """
    handoff = shot.get('handoff')
    if not handoff:
        return None, None
    source = next((row for row in board['shots'] if row['id'] == handoff['from_shot']), None)
    if source is None:
        raise ValueError('衔接帧来源镜头不属于本场')
    frames = source.get('keyframes') or []
    side = handoff.get('ref_frame', 'last')
    frame = (frames[-1] if side == 'last' else frames[0]) if frames else None
    sketch = (frame or {}).get('sketch_path')
    return ({'from_shot': source['id'], 'ref_frame': side, 'frame_id': (frame or {}).get('id'),
             'sha256': _file_sha256(sketch, '衔接帧参考图') if sketch else None},
            Path(sketch) if sketch else None)


def changed_basis_keys(recorded, current):
    """两个依据差在哪些键上；报错要能让人判断「到底哪儿变了」。"""
    return [key for key in sorted(set(recorded) | set(current)) if recorded.get(key) != current.get(key)]


def shot_light_scene(ops, shot, unit_id, scene=None):
    """这一镜的预演场景：调用方给了 ``scene`` 就用，否则按 ``shot.previs`` 去取。

    取不到就返回 ``None``——预演是加分项：没开预演页、工程文件不在、机位被删、这一镜
    根本没绑定，都只是「这次没有光线描述」，绝不能变成出图失败。所以这里把取值路上的
    一切都折成 ``None``，让调用方（提示词拼装）照旧出图。

    取值顺序：页面``peek``（只读，一个字节都不写）→ ``previs_tools._capture_scene``
    （页面不可达时回落 ``.director``，顺带核对场景编号）。页面优先是因为人工在预演页
    上拖过灯位之后 ``.director`` 还是旧文件，照文件说光会与画面不一致；先用 peek 是因为
    出图是**读**操作，不该顺手把预演快照的 revision 写涨——那会让分镜稿误以为布景变了。
    """
    if scene is not None:
        return scene
    previs = shot.get('previs') if isinstance(shot.get('previs'), dict) else None
    if not previs or not previs.get('camera_id'):
        return None  # 没绑定：没有「这一镜是哪台机器拍的」这件事，也就没有光位可言
    from . import previs_host

    host = previs_host.installed()
    if host is not None:
        try:
            return host.peek(unit_id)['scene']
        except (RuntimeError, KeyError, TypeError, ValueError):
            pass  # 页面刚换掉/还没就绪：落回下面那条统一取值路
    capture = getattr(ops, '_capture_scene', None)
    if not callable(capture):
        return None  # 单独用 StoryboardArtMixin（没有预演那一层）时，当作没有预演
    try:
        return capture(unit_id, previs)[0]
    except (ValueError, RuntimeError, OSError):
        return None  # 页面不可达 + 文件读不出：这一帧就是没有光位依据，不报错


def shot_light_line(ops, board, shot, offset_ms, detail):
    """这一帧的光线描述；取不到场景或场景里没灯时返回空字符串（不编光）。

    时刻按 ``shot.previs.time_offset_ms`` 换算（镜内第 N 毫秒 = 预演 time_offset_ms+N），
    与投影、运镜用的是同一条映射——不然同一场里「框图的机位」和「提示词的光」会指向
    两个不同的瞬间。
    """
    from .lighting import light_description
    from .previs_scene import camera_at

    scene = shot_light_scene(ops, shot, board['unit_id'])
    if not isinstance(scene, dict):
        return ''
    previs = shot.get('previs') if isinstance(shot.get('previs'), dict) else {}
    time_ms = (previs.get('time_offset_ms') or 0) + offset_ms
    try:
        camera = camera_at(scene, time_ms / 1000.0, previs.get('camera_id') or None)
    except (ValueError, KeyError, TypeError):
        return ''  # 机位缺失/位置与注视点重合：说不出「相对机位的方位」，就不说
    subjects = [{'position': row['position'], 'height': row.get('height')}
                for row in scene.get('entities') or []
                if isinstance(row, dict) and row.get('kind') == 'actor']
    try:
        return light_description(scene, camera, subjects, detail=detail)
    except (ValueError, KeyError, TypeError):
        return ''


class StoryboardArtMixin:
    if TYPE_CHECKING:
        # Implemented by the composited storyboard and screenplay Operations.
        def _write_storyboard(self, unit_id, revision, changes, *, render_receipt=None, expected_style=None, expected_body_revision=None, sketch_receipt=None, sketch_basis=None, media_patch=None, media_audit=None) -> dict: ...
        def _asset_db(self, *, write=False) -> Any: ...
        def get_asset(self, asset_id) -> dict: ...
        def _asset_card(self, card) -> dict: ...
        def read_storyboard(self, unit_id) -> dict: ...
        def apply_storyboard_changes(self, unit_id, revision, changes) -> dict: ...
        def read_script_unit_context(self, unit_id, dependency_refs=None, target_block_ids=None) -> dict: ...

    def generate_storyboard_sketch(self, unit_id, shot_id, offset_ms, layout, expected_revision, prompt='', simple_color=False):
        """Generate a descriptive sketch from a private blocking guide; never fall back."""
        from .storyboard_audio import preview_audio
        from .storyboard_camera import camera_view
        from .storyboard_sketch_style import sketch_render_instructions
        from .storyboard_style_provider import render_sketch_image
        from .storyboard_timing import integer, object_states

        if not isinstance(prompt, str) or type(simple_color) is not bool:
            raise ValueError('草图提示须为文本，简彩须为布尔值')
        if not isinstance(layout, dict):
            raise ValueError('构图参考须为对象')
        board = self.read_storyboard(unit_id)
        if integer(expected_revision, '修订号') != board['revision']:
            raise ValueError('分镜已修改，请刷新')
        if board['approved']:
            raise ValueError('请先解除定案再生成草图')
        shot = next((row for row in board['shots'] if row['id'] == shot_id), None)
        if not shot:
            raise ValueError('镜头不属于本场')
        if integer(offset_ms, '镜内位置') >= shot['duration_ms']:
            raise ValueError('关键帧位置须在镜头时长内')
        context = self.read_script_unit_context(unit_id)
        current_blocks = {row['id'] for row in context['unit']['blocks']}
        if any(identifier not in current_blocks for identifier in shot['source_block_ids']):
            raise ValueError('本镜正文依据已失效，请先重新选择正文块')
        basis, references = self._storyboard_sketch_basis(board, shot, offset_ms, context)
        asset_descriptions = [{key: card[key] for key in ('id', 'category', 'title', 'detail', 'prompt') if key in card}
                              for card in basis['assets']]
        # 提示词照旧用「此刻」的调度、命名时刻与声音节拍：它们该随排演变。
        # 但它们不进生成依据（见 _storyboard_sketch_basis），否则每调一次调度就判依据失效。
        action_ms = shot.get('action_start_ms', 0) + offset_ms
        objects = object_states(board, action_ms)
        moments = [{key: marker[key] for key in ('id', 'name', 'time_ms')} for marker in board.get('markers', [])]
        cues = [{key: sound[key] for key in ('id', 'kind', 'label') if key in sound}
                for sound in preview_audio(board, shot, offset_ms)]
        camera = camera_view(board, shot, offset_ms, objects)
        # 有预演就把这一帧的光写成一句话（放「画法」之后），没有预演时原样不动：
        # 同一场多镜因此共享同一盏主光的方向与硬度，逐帧出图不至于拼成几场戏。
        from .lighting import merge_into_prompt
        text = (
            merge_into_prompt(
                sketch_render_instructions(basis['sketch_style'], simple_color)
                + '以构图参考确认主体位置和景别，具体人物、物件和动作以正文与调度依据为准。',
                shot_light_line(self, board, shot, offset_ms, 'sketch'))
            + '\n镜头稿与视觉元素：' + json.dumps({key: basis[key] for key in ('description', 'shot_language', 'visual_elements', 'sound_intents', 'sound_notes')}, ensure_ascii=False)
            + '\n本镜描述：' + shot['title'] + '\n本镜正文：' + json.dumps(basis['blocks'], ensure_ascii=False)
            + '\n创作约定：' + json.dumps(basis['creative_agreement'], ensure_ascii=False)
            + '\n当时共享调度：' + json.dumps(objects, ensure_ascii=False)
            + '\n命名时刻与当前动作时间：' + json.dumps({'action_ms': action_ms, 'moments': moments}, ensure_ascii=False)
            + '\n当前声音节拍：' + json.dumps(cues, ensure_ascii=False)
            + '\n只绘制当前时刻：后续命名时刻不得提前显露，声音本身不意味着声源人物必须入画。'
            + '\n摄影机与投影（仅2.5D近似，不替代姿态、接触与复杂遮挡判断）：' + json.dumps(camera, ensure_ascii=False)
            + '\n关联资产：' + json.dumps(asset_descriptions, ensure_ascii=False)
            + '\n参考图顺序：首张是构图参考；其后图像为关联资产外观依据；衔接帧参考图也按外观依据使用，'
              '不把参考图的布局或文字画进成图。'
            + '\n本次绘制要求：' + prompt
        )
        with tempfile.TemporaryDirectory(prefix='storyboard-sketch-') as temp:
            guide = Path(temp) / 'composition-guide.png'
            output = Path(temp) / 'generated-sketch.png'
            if shot.get('camera', {}).get('keys') and objects and not layout.get('subjects'):
                render_camera_guide(layout, camera, objects).save(guide)
            else:
                render_sketch(layout).save(guide)
            receipt = render_sketch_image(guide, text, output, reference_images=references)
            # Decode the returned file before any binding; missing/broken output is fatal.
            with Image.open(output) as image:
                image.load()
                if image.width < 1 or image.height < 1:
                    raise ValueError('生成草图不是有效图片')
            return self._write_storyboard(
                unit_id, expected_revision,
                [{'op': 'set_keyframe', 'shot_id': shot_id, 'offset_ms': offset_ms, 'sketch_path': str(output)}],
                sketch_receipt={'provider': receipt, 'prompt': text, 'user_prompt': prompt, 'layout': layout, 'simple_color': simple_color},
                sketch_basis=basis,
            )

    def _storyboard_sketch_basis(self, board, shot, offset_ms, context, asset_db=None):
        """出图依据＝只收「人写下的、不随排演漂移」的输入。

        以前这里还带着当时的二维调度（object_states）、机位投影、命名时刻和正在响的
        声音节拍。它们本来就该跟着排演变，结果每调一次调度或挪一个动作关键点，出图
        就被判「依据已变化」而拒绝——「出图必被拒」的根源就在这里。

        现在只剩：本镜正文块、创作约定、项目风格、资产卡（含图片哈希）、本帧已有框图
        哈希、衔接帧引用。这些变了，才真的意味着这张图该重画。
        """
        from .storyboard_sketch_style import current_sketch_style

        assets, references = [], []
        # 资产从静态绑定取（场平面对象 + 视觉元素），不从 object_states 取：位置会漂，asset_id 不会。
        identifiers = sorted({row['asset_id'] for row in [*board.get('objects', []), *shot.get('visual_elements', [])]
                              if row.get('asset_id')})
        for identifier in identifiers:
            if asset_db is None:
                card = self.get_asset(identifier)
            else:
                row = asset_db.execute('SELECT data FROM assets WHERE id=?', (identifier,)).fetchone()
                if not row:
                    raise ValueError('生成依据资产已删除')
                card = self._asset_card(json.loads(row[0]))
            # revision 与 used_by 是给人看的派生信息（谁在用这张卡）：别处多一处绑定就会变，
            # 留着等于让别的场次把本帧的出图判成失效。
            card = {key: value for key, value in card.items() if key not in ('revision', 'used_by')}
            for field in ('images', 'reference_images'):
                for image in card.get(field, []):
                    path = Path(image['path'])
                    if not path.is_file():
                        raise ValueError('生成依据参考图缺失')
                    image['content_hash'] = hashlib.sha256(path.read_bytes()).hexdigest()
                    if path not in references:
                        references.append(path)
            assets.append(card)
        diagram = frame_diagram(shot, offset_ms)
        handoff, handoff_image = handoff_reference(board, shot)
        if handoff_image is not None and handoff_image not in references:
            references.append(handoff_image)
        return {'unit_id': board['unit_id'], 'shot_id': shot['id'], 'offset_ms': offset_ms,
                'sound_notes': shot.get('sound_notes', ''), 'description': shot.get('description', ''), 'shot_language': shot.get('shot_language', ''), 'visual_elements': shot.get('visual_elements', []), 'sound_intents': [row for row in board.get('sound_intents', []) if row['id'] in shot.get('sound_intent_ids', [])],
                'body_revision': context['unit']['body_revision'], 'sketch_style': current_sketch_style(self),
                'blocks': [row for row in context['unit']['blocks'] if row['id'] in shot['source_block_ids']],
                'creative_agreement': context.get('creative_agreement', {}), 'project_style': context['project_style'],
                'assets': assets, 'frame_diagram': diagram, 'handoff': handoff}, references

    def export_storyboard_style_package(self, unit_id, shot_id, output_path):
        board = self.read_storyboard(unit_id)
        if not board["approved"]:
            raise ValueError("请先定案本场分镜")
        shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
        if shot is None or not shot["keyframes"]:
            raise ValueError("请先为本镜绑定草图关键帧")
        context = self.read_script_unit_context(unit_id)
        paragraphs = [
            b["payload"].get("text", "")
            for b in context["unit"]["blocks"]
            if b["id"] in shot["source_block_ids"]
        ]
        frames = []
        for frame in shot["keyframes"]:
            sketch = Path(frame["sketch_path"])
            if not sketch.is_file():
                raise ValueError("草图缺失，请重新绑定")
            frames.append(
                (
                    sketch,
                    f"sketches/{frame['id']}{sketch.suffix.lower()}",
                    {"frame_id": frame["id"], "offset_ms": frame["offset_ms"]},
                )
            )
        style = context["project_style"]["text"]
        # 风格帧版带上色温、光比与暗部占比（素描版只说方向）：这一段是「把草图画成最终
        # 画面」时最容易各画各的地方。取该镜首个关键帧的时刻——一个包对应一镜，光位按
        # 这一镜开场那个瞬间说，与草图那几帧用的是同一条映射与同一个场景。
        from .lighting import merge_into_prompt
        light_line = shot_light_line(self, board, shot, frames[0][2]['offset_ms'], 'style')
        head = (
            f"镜头：{shot['title']}\n本镜正文：{' '.join(paragraphs)}\n项目风格：{style or '项目尚未指定'}"
        )
        prompt = merge_into_prompt(head, light_line) + (
            "\n以每张原草图为图像参考，保留宽高比、主体位置、动作轮廓、景别和主光方向。"
            "按项目风格逐帧出图。按清单中的关键帧编号与时间点回绑风格图。"
        )
        manifest = {
            "unit_id": unit_id,
            "shot_id": shot_id,
            "board_revision": board["revision"],
            "duration_ms": shot["duration_ms"],
            "keyframes": [m | {"sketch": name} for _, name, m in frames],
        }
        output = Path(output_path).resolve()
        if output.suffix.lower() != ".zip":
            raise ValueError("风格化制作包须为 ZIP 文件")
        # Exclusive creation preserves any existing export, including after a failure.
        with output.open("xb") as stream:
            try:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
                    for src, name, _ in frames:
                        package.write(src, name)
                    package.writestr(
                        "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                    )
                    package.writestr("prompt.txt", prompt)
            except Exception:
                stream.close()
                output.unlink(missing_ok=True)
                raise
        return str(output)
