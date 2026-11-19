"""Independent visual assets, accessed exclusively through project Operations."""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .content_errors import (
    ContentConflictError,
    ContentGuardError,
    ContentValidationError,
    content_item,
    content_write,
)
from .graph_store import GraphStore, active_connection, on_rollback
from .node_material import ALLOWED_SUFFIXES, MAX_IMAGE_BYTES, _as_path, _position
from .readable_ids import allocate_id

CATEGORIES = {"appearance": "person", "item": "item", "location": "location", "material": None}
ASSET_PREFIXES = {"appearance": "appearance-", "item": "item-design-", "location": "location-design-", "material": "material-"}

# 布景（预演的房间/盒体/机位）是地点资产的一个字段：一个地点一套布景，多个场共用同一套。
# 盒体的 kind 与预演页同一套说法；这里不复用 ``previs_tools`` 的常量——资产层不该为了
# 一个元组把整条分镜/预演 import 链拖进来，那会让资产库的读写也依赖预演页。
SET_BLOCK_KINDS = ('prop', 'actor')
SET_BLOCK_FIELDS = ('kind', 'x', 'z', 'y', 'w', 'd', 'h', 'rotation')
SET_CAMERA_FIELDS = ('camera_id', 'name', 'position', 'target', 'focal', 'framing')
DEFAULT_ACTOR_HEIGHT = 1.75
DEFAULT_BLOCK_HEIGHT = 1.0


def _set_number(value, label, *, positive=False):
    """布景里的数值：拒绝 bool（它是 int 的子类）与非有限值，免得 NaN 进库后每处都要防。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label}须为有限数值，实际是{value!r}')
    number = float(value)
    if positive and not number > 0:
        raise ValueError(f'{label}须大于0，实际是{value!r}')
    return number


def _set_vec3(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f'{label}须为[x, y, z]三元数组，实际是{value!r}')
    return [_set_number(item, f'{label}[{index}]') for index, item in enumerate(value)]


def _set_room(room):
    """房间尺寸 ``{w, d, h}``；null 表示「这套布景不声明房间」（房间仍归预演页管）。"""
    if room is None:
        return None
    if not isinstance(room, dict) or set(room) != {'w', 'd', 'h'}:
        raise ValueError('room须为{w, d, h}对象或null；尺寸单位为米')
    return {key: _set_number(room[key], f'room.{key}', positive=True) for key in ('w', 'd', 'h')}


def _set_blocks(rows, previous=()):
    """盒体 ``{kind, x, z, y?, w, d, h?, rotation?}``：米、y 向上、y 是盒体底面高度。

    为什么用「宽深高」而不是预演的 ``scale``：写布景的人想的是「这堵墙 4 米宽、2.8 米高」，
    铺场时按预演同一口径换成 ``position``/``scale``（``previs_tools`` 的盒体是
    ``scale`` 为全尺寸、底面贴 y）。

    盒体没有资产级编号（它是「一面墙」而不是一台可被镜头绑定的机位），但预演页的实体编号
    仍然要记着，否则每次铺场都会再添一份同样的墙。这里按几何签名沿用同位置的旧实体编号：
    位置尺寸都没变的墙还是原来那面；改过尺寸的墙就是新墙（旧的那面由 ``overwrite=true``
    的一次整场替换清掉）。
    """
    if not isinstance(rows, list):
        raise ValueError('blocks须为数组；要清空请显式传[]')
    prior = {}
    for row in previous if isinstance(previous, (list, tuple)) else ():
        if isinstance(row, dict):
            prior.setdefault(_block_signature(row), row.get('previs_entity_id'))
    result = []
    for index, row in enumerate(rows):
        label = f'blocks第{index + 1}项'
        if not isinstance(row, dict):
            raise ValueError(f'{label}须为对象')
        extra = set(row) - set(SET_BLOCK_FIELDS)
        if extra:
            raise ValueError(f'{label}有不认识的字段：' + '、'.join(sorted(extra))
                             + '；允许：' + '、'.join(SET_BLOCK_FIELDS))
        kind = row.get('kind')
        if kind not in SET_BLOCK_KINDS:
            raise ValueError(f'{label}的kind须为' + '/'.join(SET_BLOCK_KINDS) + f'，实际是{kind!r}')
        item = {'kind': kind, 'x': _set_number(row.get('x'), f'{label}的x'),
                'z': _set_number(row.get('z'), f'{label}的z'),
                'y': _set_number(row.get('y', 0.0), f'{label}的y'),
                'w': _set_number(row.get('w'), f'{label}的w', positive=True),
                'd': _set_number(row.get('d'), f'{label}的d', positive=True)}
        if row.get('h') is not None:
            item['h'] = _set_number(row.get('h'), f'{label}的h', positive=True)
        else:
            # 省略高度按预演默认值：人 1.75 米，道具 1 米——写布景时常只记得宽深。
            item['h'] = DEFAULT_ACTOR_HEIGHT if kind == 'actor' else DEFAULT_BLOCK_HEIGHT
        if row.get('rotation') is not None:
            # 角度制（人读方便）；铺场时换成预演的弧度。
            item['rotation'] = _set_number(row.get('rotation'), f'{label}的rotation')
        entity = prior.get(_block_signature(item))
        if isinstance(entity, str) and entity.strip():
            item['previs_entity_id'] = entity.strip()
        result.append(item)
    return result


def _block_signature(row):
    """盒体的几何签名：同位置同尺寸当作同一面墙，用来认领旧的预演实体编号。"""
    return (row.get('kind'), row.get('x'), row.get('z'), row.get('y', 0.0),
            row.get('w'), row.get('d'), row.get('h'), row.get('rotation'))


def _set_cameras(rows, previous=()):
    """机位 ``{camera_id?, name, position, target, focal, framing?}``。

    ``camera_id`` 是**资产级**标识：镜头绑定的是它，不是预演页里会变的实体编号。
    省略时先按同名沿用旧编号（改名不改身份，绑定才稳得住），再不够才按序发放。
    ``previs_entity_id`` 由程序回写，不接受手改（见 ``remember_set_entities``）。
    """
    if not isinstance(rows, list):
        raise ValueError('cameras须为数组；要清空请显式传[]')
    prior = [row for row in previous if isinstance(row, dict)]
    by_id = {row.get('camera_id'): row for row in prior}
    by_name = {str(row.get('name') or ''): row for row in prior}
    result, ids, names = [], set(), set()
    for index, row in enumerate(rows):
        label = f'cameras第{index + 1}项'
        if not isinstance(row, dict):
            raise ValueError(f'{label}须为对象')
        extra = set(row) - set(SET_CAMERA_FIELDS)
        if extra:
            raise ValueError(f'{label}有不认识的字段：' + '、'.join(sorted(extra))
                             + '；允许：' + '、'.join(SET_CAMERA_FIELDS))
        name = row.get('name')
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'{label}的name须为非空文本')
        name = name.strip()
        if name in names:
            raise ValueError('同一布景里的机位name不能重复：' + name)
        names.add(name)
        camera_id = row.get('camera_id')
        camera_id = camera_id.strip() if isinstance(camera_id, str) else ''
        if not camera_id:
            camera_id = str((by_name.get(name) or {}).get('camera_id') or '')
        if not camera_id:
            number = index + 1
            while f'cam-{number}' in ids:
                number += 1
            camera_id = f'cam-{number}'
        if camera_id in ids:
            raise ValueError('同一布景里的机位camera_id不能重复：' + camera_id)
        ids.add(camera_id)
        framing = row.get('framing')
        if framing is not None and (not isinstance(framing, str) or not framing.strip()):
            raise ValueError(f'{label}的framing须为非空文本或省略')
        item = {'camera_id': camera_id, 'name': name,
                'position': _set_vec3(row.get('position'), f'{label}的position'),
                'target': _set_vec3(row.get('target'), f'{label}的target'),
                'focal': _set_number(row.get('focal'), f'{label}的focal', positive=True)}
        if framing is not None:
            item['framing'] = framing.strip()
        # 同编号（或同名）旧机位的页面实体编号一并沿用：只挪机位不该丢掉已有映射。
        entity = (by_id.get(camera_id) or by_name.get(name) or {}).get('previs_entity_id')
        if isinstance(entity, str) and entity.strip():
            item['previs_entity_id'] = entity.strip()
        result.append(item)
    return result


def location_set_row(previous, room, blocks, cameras):
    """把一次布景写入规范成落库形状：``revision`` 与 ``updated_at`` 由程序给，不接受手改。"""
    prior = previous if isinstance(previous, dict) else {}
    return {'room': _set_room(room),
            'blocks': _set_blocks(blocks, prior.get('blocks') or []),
            'cameras': _set_cameras(cameras, prior.get('cameras') or []),
            'revision': int(prior.get('revision') or 0) + 1,
            'updated_at': datetime.now(UTC).isoformat(timespec='seconds')}


# ---------------------------------------------------------------- 资产产物流水线
# 资产先写文字稿、再出设定图、最后出工作用素描图。三步各自能单独重跑，软件不设强制闸门：
# 没有文字稿也允许出设定图（回执里说清「一致性没有文字依据」），有文字稿也不强制出图。
# 只有素描图例外——它必须依据一张设定图，否则「跨镜一致」根本没有对象可比。
#
# 文字稿的段落按「图要用到的信息」分，不按资料卡的栏目分：每一段都能直接变成画面要求。
DRAFT_SECTIONS = ('appearance', 'costume', 'prop', 'location', 'proportion', 'marks')
# 单段封顶：文字稿是给图和模型看的要点，写成长篇资料卡只会把关键识别特征埋掉。
MAX_DRAFT_SECTION_CHARS = 2000
# 考据引文的批次上限与 `source_location.locate_text` 的口径一致（一千条、合计二十万字）：
# 同一句原文在两处都该被同样地接受或拒绝，不该因为写的是文字稿就换一套标准。
# 考据批次上限：它现在是「外部依据清单」而不是原文引文，条数不必卡太死，
# 但仍要有上限，免得把整本书贴进资产卡。
MAX_DRAFT_RESEARCH = 200
MAX_DRAFT_RESEARCH_CHARS = 20000
# 各类别允许写哪些段，按类别收窄：人物不该写地点、道具不该写服装——写进去的段落图用不上，
# 却会让「文字稿已就绪」变成假信号。材料只留外观与标志性细节（它本来就不出形象图）。
DRAFT_SECTIONS_BY_CATEGORY = {
    'appearance': ('appearance', 'costume', 'proportion', 'marks'),
    'item': ('prop', 'appearance', 'proportion', 'marks'),
    'location': ('location', 'appearance', 'marks'),
    'material': ('appearance', 'marks'),
}
# 设定图的类型与视角：一个类别出哪种图由类别决定，多视角靠分开出多张而不是挤进一帧。
DESIGN_KINDS = ('figure', 'prop', 'location', 'moodboard')
DESIGN_VIEWS = ('front', 'side', 'back', 'none')
DESIGN_KIND_BY_CATEGORY = {'appearance': 'figure', 'item': 'prop', 'location': 'location'}


def _draft_sections(category, sections):
    """校验并规范化文字稿的段落：键在白名单内、类别匹配、值非空且不过长。"""
    allowed = DRAFT_SECTIONS_BY_CATEGORY.get(category)
    if allowed is None:
        raise ValueError('未知资产类别，无法确定文字稿允许的段落')
    if not isinstance(sections, dict) or not sections:
        raise ValueError('sections须为非空对象，至少写一段：' + '、'.join(allowed))
    unknown = sorted(set(sections) - set(DRAFT_SECTIONS))
    if unknown:
        raise ValueError('文字稿有不认识的段落：' + '、'.join(unknown)
                         + '；允许：' + '、'.join(DRAFT_SECTIONS))
    for key, text in sections.items():
        if key not in allowed:
            raise ValueError(f'{category}资产不写{key}段；这一类允许：' + '、'.join(allowed))
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'文字稿的{key}段须为非空文本')
        if len(text.strip()) > MAX_DRAFT_SECTION_CHARS:
            raise ValueError(f'文字稿的{key}段超过{MAX_DRAFT_SECTION_CHARS}字：'
                             '文字稿是给图看的要点，不是资料卡')
    # 按白名单顺序输出，与传入顺序无关：同一份文字稿因此总是同一份 JSON。
    return {key: sections[key].strip() for key in DRAFT_SECTIONS if key in sections}


def _draft_research(rows):
    """校验考据：**可选**，记的是文化、历史、原理这类外部依据，不是作品原文。

    作品是虚构的，原文没有可考据的东西；真正需要查的是「1940 年代上海弄堂的门朝哪开」
    「宋代文官袍色怎么分」「枪机后坐是什么原理」这类现实依据。所以每条只要求「主题 +
    结论」，来源（书名、纪录片、观察）可有可无；一条都不写也完全成立。
    """
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError('research须为[{topic, note, basis}]数组；不写考据就省略或传[]')
    if len(rows) > MAX_DRAFT_RESEARCH:
        raise ValueError(f'考据最多{MAX_DRAFT_RESEARCH}条')
    result, total = [], 0
    for index, row in enumerate(rows):
        label = f'research第{index + 1}条'
        if not isinstance(row, dict):
            raise ValueError(f'{label}须为对象')
        extra = set(row) - {'topic', 'note', 'basis'}
        if extra:
            raise ValueError(f'{label}有不认识的字段：' + '、'.join(sorted(extra))
                             + '；允许：topic、note、basis')
        note = row.get('note')
        if not isinstance(note, str) or not note.strip():
            raise ValueError(f'{label}的note须为非空文本（这条考据查到了什么）')
        note = note.strip()
        total += len(note)
        if total > MAX_DRAFT_RESEARCH_CHARS:
            raise ValueError(f'本批考据总长度不能超过{MAX_DRAFT_RESEARCH_CHARS}字符')
        item = {'note': note}
        topic = row.get('topic')
        if topic is not None:
            if not isinstance(topic, str) or not topic.strip():
                raise ValueError(f'{label}的topic须为非空文本或省略')
            item['topic'] = topic.strip()
        basis = row.get('basis')
        if basis is not None:
            if not isinstance(basis, str) or not basis.strip():
                raise ValueError(f'{label}的basis须为非空文本或省略')
            item['basis'] = basis.strip()
        result.append(item)
    return result


def draft_row(previous, category, sections, research, style_ref):
    """把一次文字稿写入规范成落库形状：``revision`` 与 ``updated_at`` 由程序给。

    整份替换而不是打补丁：文字稿是「这个人物/道具/地点长什么样」的一整套描述，一半新一半
    旧比没有更难判断；写的人手里本来就有整份（与布景同一口径）。
    """
    prior = previous if isinstance(previous, dict) else {}
    return {'sections': _draft_sections(category, sections),
            'research': _draft_research(research),
            'style_ref': dict(style_ref or {}),
            'revision': int(prior.get('revision') or 0) + 1,
            'updated_at': datetime.now(UTC).isoformat(timespec='seconds')}


class _AttachedAssetDb:
    """A narrow SQL interface: only fixed asset statements reach the attached DB."""
    _queries = {
        'SELECT data FROM assets WHERE id=?': 'SELECT data FROM narrative_assets.assets WHERE id=?',
        "SELECT value FROM meta WHERE key='revision'": "SELECT value FROM narrative_assets.meta WHERE key='revision'",
        'SELECT data FROM assets ORDER BY rowid': 'SELECT data FROM narrative_assets.assets ORDER BY rowid',
        'SELECT id FROM assets': 'SELECT id FROM narrative_assets.assets',
        'SELECT data FROM assets': 'SELECT data FROM narrative_assets.assets',
        'SELECT 1 FROM assets WHERE id=?': 'SELECT 1 FROM narrative_assets.assets WHERE id=?',
        'DELETE FROM assets WHERE id=?': 'DELETE FROM narrative_assets.assets WHERE id=?',
        "UPDATE meta SET value=? WHERE key='revision'": "UPDATE narrative_assets.meta SET value=? WHERE key='revision'",
        'INSERT INTO assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data': 'INSERT INTO narrative_assets.assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
        'INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)': 'INSERT INTO narrative_assets.audit(revision,action,asset_id,summary) VALUES(?,?,?,?)',
        'SELECT value FROM meta WHERE key=?': 'SELECT value FROM narrative_assets.meta WHERE key=?',
        'INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value': 'INSERT INTO narrative_assets.meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
    }

    def __init__(self, connection):
        self.connection = connection

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def execute(self, sql, parameters=()):
        statement = self._queries.get(' '.join(sql.split()))
        if statement is None:
            raise RuntimeError('资产事务只允许明确限定资产库的已注册SQL')
        return self.connection.execute(statement, parameters)


def _initialize_asset_tables(db, schema):
    if schema not in {'main', 'narrative_assets'}:
        raise RuntimeError('未知资产数据库域')
    db.execute(f'CREATE TABLE IF NOT EXISTS {schema}.assets(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.execute(f'CREATE TABLE IF NOT EXISTS {schema}.meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL)')
    db.execute(f"INSERT OR IGNORE INTO {schema}.meta VALUES('revision',0)")
    db.execute(f'CREATE TABLE IF NOT EXISTS {schema}.audit(revision INTEGER PRIMARY KEY, action TEXT NOT NULL, asset_id TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')


class AssetOperationsMixin:
    _store: GraphStore

    def _asset_root(self) -> Path:
        from .project_package import layout
        return layout(self._store.path).material / "assets"

    @contextmanager
    def _asset_db(self, *, write=False):
        root = self._asset_root()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / 'assets.sqlite3').resolve()
        shared = active_connection(self._store.path)
        if shared is not None:
            attached = {row[1]: Path(row[2]).resolve() for row in shared.execute('PRAGMA database_list') if row[2]}
            if 'narrative_assets' in attached and attached['narrative_assets'] != path:
                raise ContentGuardError('共享事务的资产数据库归属不匹配')
            if 'narrative_assets' not in attached:
                shared.execute('ATTACH DATABASE ? AS narrative_assets', (str(path),))
            _initialize_asset_tables(shared, 'narrative_assets')
            yield cast(sqlite3.Connection, _AttachedAssetDb(shared))
            return
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            _initialize_asset_tables(db, 'main')
            db.commit()
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _asset_snapshot(self, domain):
        method = {"adaptation": "adaptation_snapshot", "extraction": "graph_snapshot"}.get(domain)
        if method is None:
            raise ValueError("资产数据域须为 adaptation 或 extraction")
        return getattr(self, method)()

    def asset_moments(self, domain="adaptation") -> list[dict]:
        snapshot = self._asset_snapshot(domain)
        nodes = [node for node in snapshot["nodes"] if node.get("node_type") in
                 {"storyline", "big_event", "scene", "event", "action"}
                 and not node.get("suppressed") and not node.get("retracted")]
        if domain == "adaptation":
            return sorted(nodes, key=lambda node: node["sort_id"])
        return sorted(nodes, key=lambda node: _position(node["id"], snapshot) or (-1, -1))

    def _asset_position(self, moment, snapshot, domain):
        if not moment:
            return None
        node = next((node for node in snapshot["nodes"] if node["id"] == moment
                     and not node.get("suppressed") and not node.get("retracted")), None)
        if node is None or node.get("node_type") not in {"storyline", "big_event", "scene", "event", "action"}:
            raise ValueError(f"资产时间点不存在：{moment}")
        return node["sort_id"] if domain == "adaptation" else _position(moment, snapshot)

    def _asset_card(self, card):
        root = self._asset_root()
        result = dict(card)
        for field in ("images", "reference_images"):
            result[field] = [image | {"path": str(root / image["file"])}
                             for image in card.get(field, [])]
        # 设定图与素描图跟参考图同一套存法：库里存相对 file，读出来补绝对 path，工程整体
        # 搬走后记录仍然指得到文件。列表只在卡里真的有这类产物时才补：老卡因此与改动前
        # 逐字节相同，不会让已生成草图的「出图依据」凭空变成「已变化」。
        for field in ("designs", "sketches"):
            rows = card.get(field)
            if isinstance(rows, list):
                result[field] = [row | {"path": str(root / row["file"])}
                                 for row in rows if row.get("file")]
        for row in result.get("sketches") or []:
            if row.get("design_path"):
                row["design_path"] = str(root / row["design_path"])
        return result

    def get_asset(self, asset_id: str) -> dict:
        with self._asset_db() as db:
            row = db.execute("SELECT data FROM assets WHERE id=?", (asset_id,)).fetchone()
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
        if not row:
            raise ContentGuardError("资产不存在")
        card = self._asset_card(json.loads(row[0])) | {"revision": revision}
        # 谁在用它：改删之前先看这里，才知道「还被引用」具体指哪几处。
        card["used_by"] = self.asset_usage(asset_id)
        return card

    def asset_usage(self, asset_id: str = "") -> list[dict]:
        """资产被谁用着：扫全部分镜场次，给出手上的绑定位置。

        资产改删之前先看这里——「还被引用」这句话只有配上位置才有用。
        """
        found = []
        with self._store.connect() as db:
            for key, value in db.execute("SELECT key, value FROM meta WHERE key LIKE 'storyboard:%'"):
                board = json.loads(value)
                unit_id = board.get('unit_id') or key.split(':', 1)[1]
                for shot in board.get('shots') or []:
                    for index, element in enumerate(shot.get('visual_elements') or []):
                        if element.get('asset_id') and (not asset_id or element['asset_id'] == asset_id):
                            found.append({'unit_id': unit_id, 'shot_id': shot.get('id'), 'shot_title': shot.get('title'),
                                          'index': index, 'kind': element.get('kind'), 'name': element.get('name'),
                                          'asset_id': element['asset_id']})
                for obj in board.get('objects') or []:
                    if obj.get('asset_id') and (not asset_id or obj['asset_id'] == asset_id):
                        found.append({'unit_id': unit_id, 'object_id': obj.get('id'), 'kind': 'object',
                                      'name': obj.get('name'), 'asset_id': obj['asset_id']})
        return found

    def _location_card(self, asset_id: str) -> dict:
        """取地点资产卡：别的类别没有布景这回事，越早拒绝越省事。"""
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ContentGuardError('请提供地点资产编号')
        card = self.get_asset(asset_id.strip())
        if card.get('category') != 'location':
            raise ContentGuardError('只有地点资产才有布景；' + str(card.get('title') or card['id'])
                                    + ' 的类别是' + str(card.get('category')))
        return card

    def location_set_usage(self, asset_id: str) -> list[dict]:
        """这套布景被哪些场用着。改布景之前先看这里——「三场共用」得说得出是哪三场。"""
        from .asset_needs import unit_set_status

        rows = []
        with self._store.connect() as db:
            for key, value in db.execute("SELECT key, value FROM meta WHERE key LIKE 'storyboard:%'"):
                board = json.loads(value)
                ref = board.get('set_ref')
                if not isinstance(ref, dict) or ref.get('asset_id') != asset_id:
                    continue
                rows.append({'unit_id': board.get('unit_id') or key.split(':', 1)[1],
                             'set_ref': {'asset_id': ref.get('asset_id'), 'revision': ref.get('revision')},
                             'status': unit_set_status(board, self.get_asset)['status']})
        rows.sort(key=lambda row: row['unit_id'])
        return rows

    def read_location_set(self, asset_id: str) -> dict:
        """只读地点资产的布景与共用它的场；没写过布景时 ``set`` 为 null。"""
        card = self._location_card(asset_id)
        return {'asset_id': card['id'], 'title': card.get('title', ''), 'category': 'location',
                'set': card.get('set'), 'asset_revision': card.get('revision'),
                'used_by': self.location_set_usage(card['id'])}

    @content_write
    def save_location_set(self, asset_id: str, blocks, cameras, room=None) -> dict:
        """写一套地点布景（整份替换），``set.revision`` 由程序 +1。

        为什么整份替换而不是打补丁：布景是「这个房间长什么样」的一整套描述，一半新一半旧
        比没有更难判断；写的人手里本来就有整份。

        revision 不接受手改：它是场的依赖依据（``unit.set_ref.revision``）。手写一个更大的
        数就能让「绑的版本早于资产当前版本」这个判断失真，所以只由程序自增。
        """
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ContentGuardError('请提供地点资产编号')
        with self._asset_db(write=True) as db:
            row = db.execute("SELECT data FROM assets WHERE id=?", (asset_id.strip(),)).fetchone()
            if row is None:
                raise ContentGuardError('资产不存在')
            card = json.loads(row[0])
            if card.get('category') != 'location':
                raise ContentGuardError('只有地点资产才有布景；' + str(card.get('title') or card['id'])
                                        + ' 的类别是' + str(card.get('category')))
            previous = card.get('set') if isinstance(card.get('set'), dict) else None
            card['set'] = location_set_row(previous, room, blocks, cameras)
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0] + 1
            db.execute("INSERT INTO assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (card['id'], json.dumps(card, ensure_ascii=False)))
            db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
            db.execute("INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)",
                       (revision, 'location_set', card['id'], card.get('title') or card['id']))
        return {'asset_id': card['id'], 'title': card.get('title', ''), 'set': card['set'],
                'set_revision': card['set']['revision'], 'asset_revision': revision,
                'used_by': self.location_set_usage(card['id'])}

    def remember_set_entities(self, asset_id: str, camera_entities: dict,
                              block_entities=None) -> dict:
        """把预演页生成的实体编号记回布景：机位记在 ``cameras[i].previs_entity_id``，
        盒体记在 ``blocks[i].previs_entity_id``（键为盒体序号）。

        为什么要记：页面每次新增实体都自己发号，而这个号是镜头绑定（``shot.previs``）与
        后续铺场要用的；机位在资产里的身份是 ``camera_id``，两者必须有人在中间搭一次桥。
        记过之后下一次铺场就带着号直接 ``setCamera``/``update``，多场共用的机位与墙不会
        每铺一次换一次身份、也不会重复堆一份。

        为什么不涨 ``set.revision``：这只是「这台机位/这面墙在预演页叫什么」的回写，布景内容
        一个字没变；涨了会让刚铺完的场立刻变成 stale，等于自己给自己发告警。资产库的全局
        revision 仍然 +1 并留审计——数据确实动过，乐观并发要看得见。
        """
        camera_entities = camera_entities if isinstance(camera_entities, dict) else {}
        block_entities = block_entities if isinstance(block_entities, dict) else {}
        if not camera_entities and not block_entities:
            return {'cameras': [], 'blocks': []}
        wrote = {'cameras': [], 'blocks': []}
        with self._asset_db(write=True) as db:
            row = db.execute("SELECT data FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                return wrote
            card = json.loads(row[0])
            if not isinstance(card.get('set'), dict):
                return wrote
            for camera in card['set'].get('cameras') or []:
                entity = camera_entities.get(camera.get('camera_id'))
                if isinstance(entity, str) and entity and camera.get('previs_entity_id') != entity:
                    camera['previs_entity_id'] = entity
                    wrote['cameras'].append({'camera_id': camera['camera_id'], 'previs_entity_id': entity})
            for index, block in enumerate(card['set'].get('blocks') or []):
                entity = block_entities.get(index)
                if isinstance(entity, str) and entity and block.get('previs_entity_id') != entity:
                    block['previs_entity_id'] = entity
                    wrote['blocks'].append({'index': index, 'previs_entity_id': entity})
            if wrote['cameras'] or wrote['blocks']:
                revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0] + 1
                db.execute("INSERT INTO assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (asset_id, json.dumps(card, ensure_ascii=False)))
                db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
                db.execute("INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)",
                           (revision, 'location_set_entity', asset_id, '回写布景实体编号'))
        return wrote

    def search_assets(self, keyword="", category="", node_id="", domain="adaptation",
                      has_images=None, unbound=False) -> dict:
        """按关键词/类别/节点/有无图/是否被用 查找资产，返回紧凑行。

        与 `list_assets` 的分工：这里不要求指定类别，用于「这个角色有没有现成资产」这类查找；
        `list_assets` 仍按类别与改编时间点做完整读取。
        """
        for value, name in ((keyword, 'keyword'), (category, 'category'), (node_id, 'node_id')):
            if not isinstance(value, str):
                raise ContentGuardError(f'{name}须为文本')
        if category and category not in CATEGORIES:
            raise ContentGuardError("未知资产类别，无法确定资产路由")
        if domain not in ('adaptation', 'extraction'):
            raise ContentGuardError('资产域须明确为adaptation或extraction')
        if has_images not in (None, True, False):
            raise ContentGuardError('has_images 须为布尔值')
        if not isinstance(unbound, bool):
            raise ContentGuardError('unbound 须为布尔值')
        with self._asset_db() as db:
            cards = [json.loads(row[0]) for row in db.execute("SELECT data FROM assets ORDER BY rowid")]
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
        usage = {}
        for row in self.asset_usage():
            usage.setdefault(row['asset_id'], []).append(row)
        needle = keyword.strip().lower()
        rows = []
        for card in cards:
            if category and card.get('category') != category:
                continue
            if card.get('category') != 'material' and card.get('domain', 'adaptation') != domain:
                continue
            if node_id and card.get('node_id') != node_id:
                continue
            if needle and needle not in f"{card.get('title', '')}\n{card.get('detail', '')}".lower():
                continue
            images = card.get('images') or []
            if has_images is True and not images:
                continue
            if has_images is False and images:
                continue
            used = usage.get(card['id'], [])
            if unbound and used:
                continue
            rows.append({'id': card['id'], 'title': card.get('title', ''), 'category': card.get('category'),
                         'node_id': card.get('node_id', ''), 'domain': card.get('domain', 'adaptation'),
                         'valid_from': card.get('valid_from', ''), 'valid_to': card.get('valid_to', ''),
                         'image_count': len(images), 'reference_count': len(card.get('reference_images') or []),
                         'used_count': len(used)})
        rows.sort(key=lambda row: (row['category'] or '', row['title']))
        return {'assets': rows, 'count': len(rows), 'revision': revision,
                'filters': {'keyword': keyword, 'category': category, 'node_id': node_id,
                            'domain': domain, 'has_images': has_images, 'unbound': unbound}}

    def update_asset(self, asset_id: str, expected_revision=None, changes=None, image_changes=None) -> dict:
        """局部修改资产：字段与图片分开改，不用重发整张卡。"""
        if not isinstance(asset_id, str) or not asset_id:
            raise ContentGuardError('请提供资产编号')
        changes = dict(changes or {})
        allowed = {'title', 'node_id', 'category', 'detail', 'prompt', 'valid_from', 'valid_to'}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ContentValidationError('不允许修改的字段：' + '、'.join(unknown),
                                         problems=[{'message': '不允许修改的字段', 'path': ['changes', key]} for key in unknown])
        card = self.get_asset(asset_id)
        payload = {'id': asset_id, **changes}

        def current(field):
            return [{'id': image['id'], 'caption': image.get('caption') or ''} for image in card.get(field) or []]

        for field, operations in (('images', image_changes), ('reference_images', None)):
            if not operations and field == 'images':
                continue
            if operations is None:
                break
            entries = current(field)
            caption_field = field
            for number, operation in enumerate(operations):
                if not isinstance(operation, dict) or operation.get('op') not in {'add', 'adopt', 'remove'}:
                    raise ContentValidationError('图片操作须为 add/adopt/remove',
                                                 problems=[{'message': '图片操作须为 add/adopt/remove',
                                                            'path': ['image_changes', number]}])
                if operation.get('field', 'images') != caption_field:
                    raise ContentValidationError('第一版只支持 images 字段的图片操作',
                                                 problems=[{'message': '图片操作只支持 images',
                                                            'path': ['image_changes', number, 'field']}])
                image_id = operation.get('image_id')
                if operation['op'] == 'add':
                    entries.append({'path': operation.get('path'), 'caption': operation.get('caption') or ''})
                elif not any(entry['id'] == image_id for entry in entries):
                    raise ContentValidationError('图片不属于该资产',
                                                 problems=[{'message': '图片不属于该资产',
                                                            'path': ['image_changes', number, 'image_id']}])
                elif operation['op'] == 'adopt':
                    entries.sort(key=lambda entry: entry['id'] != image_id)
                else:
                    entries = [entry for entry in entries if entry['id'] != image_id]
            payload[field] = entries
        return self.save_asset(payload, expected_revision=expected_revision)

    def bind_storyboard_element(self, unit_id: str, shot_id: str, index: int, asset_id: str,
                                expected_revision=None) -> dict:
        """把已有资产绑到某镜的某个元素；类别不符、已定案一律拒绝。"""
        from .storyboard_assets import KINDS

        board = self.read_storyboard(unit_id)
        if board.get('approved'):
            raise ContentGuardError('请先解除定案再改镜头元素')
        shot = next((row for row in board['shots'] if row['id'] == shot_id), None)
        if shot is None:
            raise ContentGuardError('镜头不属于本场')
        rows = [dict(element) for element in shot.get('visual_elements') or []]
        if type(index) is not int or index < 0 or index >= len(rows):
            raise ContentGuardError('元素下标超出本镜元素范围')
        card = self.get_asset(asset_id)
        expected = KINDS.get(rows[index].get('kind'))
        if expected is None or card.get('category') != expected:
            raise ContentGuardError('元素类型与资产类别不符：' + str(rows[index].get('kind')))
        rows[index]['asset_id'] = asset_id
        revision = board['revision'] if expected_revision is None else expected_revision
        result = self.apply_storyboard_changes(unit_id, revision, [{'op': 'update_shot', 'shot_id': shot_id, 'visual_elements': rows}])
        return result | {'bound': {'unit_id': unit_id, 'shot_id': shot_id, 'index': index, 'asset_id': asset_id}}

    def unbind_storyboard_element(self, unit_id: str, shot_id: str, index: int, expected_revision=None) -> dict:
        board = self.read_storyboard(unit_id)
        if board.get('approved'):
            raise ContentGuardError('请先解除定案再改镜头元素')
        shot = next((row for row in board['shots'] if row['id'] == shot_id), None)
        rows = [dict(element) for element in (shot or {}).get('visual_elements') or []]
        if type(index) is not int or index < 0 or index >= len(rows):
            raise ContentGuardError('元素下标超出本镜元素范围')
        rows[index]['asset_id'] = None
        revision = board['revision'] if expected_revision is None else expected_revision
        return self.apply_storyboard_changes(unit_id, revision, [{'op': 'update_shot', 'shot_id': shot_id, 'visual_elements': rows}])

    def generate_asset_sketch(self, asset_id: str = "", node_id: str = "", prompt: str = "") -> dict:
        """用图像服务按描述型素描生成资产形象图，写回资产卡；已有图时新增而不是覆盖。"""
        from .storyboard_sketch_style import current_sketch_style, sketch_render_instructions
        from .storyboard_style_provider import render_sketch_image

        if not asset_id and not node_id:
            raise ContentGuardError('请提供资产编号或节点编号')
        if asset_id:
            card = self.get_asset(asset_id)
        else:
            found = self.search_assets(node_id=node_id)
            if not found['assets']:
                raise ContentGuardError('该节点还没有资产卡，请先建立')
            if len(found['assets']) > 1:
                raise ContentGuardError('该节点有多张资产卡，请指定资产编号')
            card = self.get_asset(found['assets'][0]['id'])
        if card.get('category') == 'material':
            raise ContentGuardError('材质资产不生成形象素描')
        text = (sketch_render_instructions(current_sketch_style(self), False)
                + '\n资产类别：' + str(card.get('category'))
                + '\n名称：' + str(card.get('title'))
                + '\n说明：' + str(card.get('detail') or '')
                + ('\n已有提示词：' + str(card.get('prompt')) if card.get('prompt') else '')
                + '\n本次要求：' + (prompt or '画这张资产的可识别外观，供分镜与关键帧复用'))
        references = [image['path'] for image in card.get('reference_images') or [] if image.get('path')]
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix='asset-sketch-') as temp:
            # 资产图没有机位草图可给，但仍然需要一张画布当输入：空白而不是伪造构图。
            guide = Path(temp) / 'blank.png'
            Image.new('RGB', (1024, 1024), 'white').save(guide)
            output = Path(temp) / 'asset-sketch.png'
            receipt = render_sketch_image(guide, text, output, reference_images=references)
            with Image.open(output) as image:
                image.load()
            updated = self.update_asset(card['id'], expected_revision=card['revision'],
                                        image_changes=[{'op': 'add', 'path': str(output), 'caption': '生成素描'}])
        return {'asset': updated, 'generated': {'provider': receipt, 'prompt': text}}

    def _asset_style_basis(self):
        """写稿与出图共用的「风格约定」口径：一次读取，文本＋版本号。

        取的是工程默认（资产不挂在某一部剧本上，所以不吃剧本级覆盖）。为什么两个版本号
        来自同一个计数：项目风格正文存在创作约定的 ``other`` 字段里，`save_project_style`
        涨的就是创作约定的修订。这里分开记下两项，是因为文字稿与设定图依赖的是两个不同的
        问题（「照哪版措辞写的」「照哪版约定画的」）；哪天两者拆成不同计数，旧稿的
        ``style_ref`` 仍然读得懂，不需要迁移。
        """
        from .project_style import read_defaults, render_agreement

        with self._store.connect() as db:
            defaults = read_defaults(db)
        return {'text': render_agreement(defaults['fields']),
                'revision': defaults['revision'],
                'style_ref': {'project_style_version': defaults['revision'],
                              'agreement_version': defaults['revision']}}

    @content_write
    def write_asset_draft(self, asset_id: str, sections, research=None, sources=None) -> dict:
        """写一张资产的文字稿（整份替换），``draft.revision`` 由程序 +1。

        为什么先写文字稿：外观、服装、材质、比例、标志性细节这些「这张图必须画成什么样」
        的判断，只有在文字里才说得清、改得动、核得起来；直接出图就等于把这些判断交给一次
        随机采样，下一张图与上一张对不上时也无从追责。

        为什么写稿时记下风格与约定版本：文字稿是「依项目风格与创作约定＋原文考据」写的，
        出图时那两条可能已经被改过；不记下来，日后就看不出这稿是照哪一版写的。
        """
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ContentGuardError('请提供资产编号')
        style_ref = self._asset_style_basis()['style_ref']
        with self._asset_db(write=True) as db:
            row = db.execute("SELECT data FROM assets WHERE id=?", (asset_id.strip(),)).fetchone()
            if row is None:
                raise ContentGuardError('资产不存在')
            card = json.loads(row[0])
            card['draft'] = draft_row(card.get('draft'), card.get('category'), sections,
                                      research if research is not None else sources, style_ref)
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0] + 1
            db.execute("INSERT INTO assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (card['id'], json.dumps(card, ensure_ascii=False)))
            db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
            db.execute("INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)",
                       (revision, 'asset_draft', card['id'], card.get('title') or card['id']))
        return {'asset_id': card['id'], 'title': card.get('title', ''), 'category': card.get('category'),
                'draft': card['draft'], 'draft_revision': card['draft']['revision'],
                'style_ref': card['draft']['style_ref'], 'asset_revision': revision}

    def run_asset_generation_task(self, asset_id, kind='asset_design', prompt='', view=None,
                                  design_path=None, constraints=None) -> dict:
        """资产流水线上的一次真实出图：设定图或素描图，成功后写回资产卡。

        每步都能单独重跑，没有强制闸门：没有文字稿也允许出设定图（回执带
        ``DRAFT_MISSING``），有文字稿也不强制必须出图。只有素描图例外——它必须以一张设定图
        为依据，缺了就在调服务之前报错：工作用线稿的全部意义就是「照着已定的形体再画一次」，
        没有形体依据时生成出来的东西没法当参考。

        为什么产物追加而不是覆盖：设定图本来就要多张（正面/侧面/背面、氛围图），素描图则要
        留「依据哪张设定图出的」这层关系；覆盖会让回溯断掉。因为只追加、不改旧记录，这里也
        不需要乐观并发参数。
        """
        from . import generation_task

        # 回执摘要沿用分镜那条口径（同一批 provider、同一份字段名单）：这里不复刻一份。
        from .storyboard_operations import _provider_summary

        if kind not in generation_task.ASSET_KINDS:
            raise ContentGuardError('资产出图类型须为' + '、'.join(generation_task.ASSET_KINDS))
        if not isinstance(prompt, str):
            raise ContentGuardError('本次补充要求须为文本')
        if view is not None and view not in DESIGN_VIEWS:
            raise ContentGuardError('view须为' + '、'.join(DESIGN_VIEWS) + '或省略')
        card = self.get_asset(asset_id)
        if card.get('category') not in DESIGN_KIND_BY_CATEGORY:
            raise ContentGuardError('材质资产不生成设定图或素描图')
        style = self._asset_style_basis()
        skill = generation_task.select_skill(kind)
        references = [str(image['path']) for field in ('images', 'reference_images')
                      for image in card.get(field) or [] if image.get('path')]
        sketch_style = ''
        if kind == 'asset_sketch':
            from .storyboard_sketch_style import current_sketch_style, sketch_render_instructions

            sketch_style = sketch_render_instructions(current_sketch_style(self), False)
        task = generation_task.build_task(kind, asset=card, style={'text': style['text'], 'references': references},
                                          prompt=prompt, view=view, design_path=design_path,
                                          sketch_style=sketch_style, constraints=constraints)
        prompt_text = generation_task.task_prompt(task)
        with tempfile.TemporaryDirectory(prefix=f'asset-{kind}-') as folder:
            receipt = generation_task.execute(task, skill, work_dir=Path(folder))
            written = self._record_asset_artifact(card['id'], kind, task, receipt, style['revision'], prompt_text)
        return {'asset_id': card['id'], 'kind': kind, 'title': card.get('title', ''),
                'draft_revision': (task.get('draft') or {}).get('revision', 0),
                'asset_revision': written['asset_revision'], 'record': written['record'],
                'skill': receipt['skill'], 'warnings': receipt['warnings'],
                'provider': _provider_summary(receipt['provider_receipt']),
                'started_at': receipt['started_at'], 'finished_at': receipt['finished_at'],
                'task': task}

    def _record_asset_artifact(self, asset_id, kind, task, receipt, style_version, prompt_text):
        """把一次成功的出图追加进 designs/sketches；失败就不写（回执都没拿到，这里不会进）。"""
        source = receipt.get('output_path')
        if not source or not Path(source).is_file():
            # 执行器已经验证过图片可读；走到这里说明产物在回收临时目录时丢了，不写空记录。
            raise ContentGuardError('生成产物不可读，未写入资产卡')
        provider = receipt.get('provider_receipt') if isinstance(receipt.get('provider_receipt'), dict) else {}
        field, prefix = {'asset_design': ('designs', 'asset-design-'),
                         'asset_sketch': ('sketches', 'asset-sketch-')}[kind]
        # 产物类型由资产类别决定（人物 figure、道具 prop、地点 location），不进任务包：
        # 它描述的是「这条记录是什么图」，不是「这次要生成什么」。
        record = {'kind': DESIGN_KIND_BY_CATEGORY[task['category']], 'view': task['view'],
                  'prompt': prompt_text,
                  'provider': str(provider.get('provider') or ''), 'job_id': str(provider.get('job_id') or ''),
                  'draft_revision': int((task.get('draft') or {}).get('revision') or 0),
                  'style_version': int(style_version),
                  'created_at': datetime.now(UTC).isoformat(timespec='seconds')}
        if kind == 'asset_sketch':
            # 记下依据的是哪一张设定图：素描图的价值全在这条关系上（改了设定图，旧线稿
            # 就该重画），存相对 file 是为了工程搬走后还指得到。
            design = task.get('design') or {}
            record['design_path'] = str(design.get('file') or '')
        root = self._asset_root()
        suffix = Path(source).suffix.lower() or '.png'
        with ExitStack() as rollback:
            with self._asset_db(write=True) as db:
                row = db.execute("SELECT data FROM assets WHERE id=?", (asset_id,)).fetchone()
                if row is None:
                    raise ContentGuardError('资产不存在')
                stored = json.loads(row[0])
                rows = stored.get(field) if isinstance(stored.get(field), list) else []
                artifact_id = allocate_id(db, "assets", prefix)
                target = root / asset_id / (artifact_id + suffix)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    rollback.callback(target.unlink, missing_ok=True)
                    on_rollback(self._store.path, lambda path=target: path.unlink(missing_ok=True))
                    with Path(source).open('rb') as original:
                        shutil.copyfileobj(original, output)
                stored[field] = [*rows, {'file': target.relative_to(root).as_posix(), **record}]
                revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0] + 1
                db.execute("INSERT INTO assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (asset_id, json.dumps(stored, ensure_ascii=False)))
                db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
                db.execute("INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)",
                           (revision, kind, asset_id, stored.get('title') or asset_id))
            rollback.pop_all()
        # 返回给调用方的记录与读视图同一形状：路径一律绝对，库里存的是相对 file。
        shown = {'path': str(target), **record}
        if shown.get('design_path'):
            shown['design_path'] = str(root / shown['design_path'])
        return {'record': shown, 'asset_revision': revision}

    def list_assets(self, category: str, moment="", node_id="", domain="adaptation") -> dict:
        if category not in CATEGORIES:
            raise ContentGuardError("未知资产类别，无法确定资产路由")
        snapshot = self._asset_snapshot(domain)
        current = self._asset_position(moment, snapshot, domain) if category != "material" else None
        with self._asset_db() as db:
            cards = [json.loads(row[0]) for row in db.execute("SELECT data FROM assets ORDER BY rowid")]
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
        shown, diagnostics = [], []
        for card in cards:
            if card["category"] != category or (category != "material" and card["domain"] != domain):
                continue
            if node_id and card["node_id"] != node_id:
                continue
            try:
                start = self._asset_position(card["valid_from"], snapshot, domain)
                end = self._asset_position(card["valid_to"], snapshot, domain)
                if start is not None and end is not None and start >= end:
                    raise ValueError("资产时间区间倒置，请调整生效或失效时间")
                if category != "material" and ((current is None and end is not None) or
                    (current is not None and ((start is not None and current < start) or
                                             (end is not None and current >= end)))):
                    continue
            except ValueError as exc:
                diagnostics.append({"asset_id": card["id"], "message": str(exc)})
                continue
            shown.append(self._asset_card(card))
        return {"assets": shown, "revision": revision, "category": category,
                "moment": moment, "node_id": node_id, "domain": domain, "diagnostics": diagnostics}

    @content_write
    def save_asset(self, payload: dict, expected_revision=None) -> dict:
        if not isinstance(payload, dict):
            raise ContentGuardError("资产路由须为明确对象")
        if 'set' in payload:
            # 布景有它自己的写入口（save_location_set）：revision 自增与校验都在那里，
            # 从这里塞一份 set 只会绕开那两条，所以宁可拒绝也不静默忽略。
            raise ContentGuardError('布景不能随资产卡一起保存：请用save_location_set写布景，revision由程序自增')
        with ExitStack() as rollback:
            with self._asset_db(write=True) as db:
                revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
                if expected_revision is not None and revision != expected_revision:
                    raise ContentConflictError("资产已改变，请刷新后重试")
                if payload.get("id"):
                    asset_id = str(payload["id"])
                else:
                    category = payload.get("category")
                    if category not in CATEGORIES:
                        raise ContentGuardError("未知资产类别，无法确定资产路由")
                    asset_id = allocate_id(db, "assets", ASSET_PREFIXES[category],
                                           (row[0] for row in db.execute("SELECT id FROM assets")))
                row = db.execute("SELECT data FROM assets WHERE id=?", (asset_id,)).fetchone()
                old = json.loads(row[0]) if row else {}
                if payload.get("id") and not row:
                    raise ContentGuardError("资产不存在")
                card = old | {key: value for key, value in payload.items() if key in
                              {"category", "node_id", "title", "detail", "domain", "valid_from", "valid_to", "prompt"}}
                card["id"] = asset_id
                for field in ("node_id", "title", "detail", "valid_from", "valid_to", "prompt"):
                    card[field] = str(card.get(field) or "")
                card.setdefault("domain", "adaptation")
                category = card.get("category")
                if category not in CATEGORIES:
                    raise ContentGuardError("未知资产类别，无法确定资产路由")
                card["title"] = card["title"].strip()
                if card['domain'] not in ('adaptation', 'extraction'):
                    raise ContentGuardError('资产域须明确为adaptation或extraction')
                snapshot = self._asset_snapshot(card["domain"])
                if category == "material":
                    if card['node_id']:
                        raise ContentGuardError('材质资产不关联节点，无法按此路由保存')
                    if card['valid_from'] or card['valid_to']:
                        raise ValueError('材质不关联时间点')
                elif card["node_id"]:
                    node = next((node for node in snapshot["nodes"] if node["id"] == card["node_id"]
                                 and not node.get("suppressed") and not node.get("retracted")), None)
                    if node is None or node.get("node_type") != CATEGORIES[category]:
                        raise ContentGuardError("资产须关联对应类别及所选域中的有效节点")
                if not card["title"]:
                    raise ContentValidationError('资产标题不能为空', problems=[{'code': 'CONTENT_VALIDATION', 'message': '资产标题不能为空', 'path': ['payload', 'title']}])
                start = self._asset_position(card["valid_from"], snapshot, card["domain"])
                end = self._asset_position(card["valid_to"], snapshot, card["domain"])
                if start is not None and end is not None and start >= end:
                    raise ValueError("失效时间须在生效时间之后")
                prepared = {field: self._prepare_asset_images(payload.get(field), old.get(field, []), field)
                            for field in ('images', 'reference_images')}
                for field in ('images', 'reference_images'):
                    card[field] = self._asset_images(db, asset_id, prepared[field], rollback)
                revision += 1
                db.execute("INSERT INTO assets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (asset_id, json.dumps(card, ensure_ascii=False)))
                db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
                db.execute("INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)",
                           (revision, "update" if row else "create", asset_id, card["title"]))
                result = self._asset_card(card) | {'revision': revision}
            rollback.pop_all()
            return result

    def _prepare_asset_images(self, entries, old, field):
        if entries is None:
            return old
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("图片须为对象数组")
        existing = {image["id"]: image for image in old}
        prepared = []
        # Validate every source before importing any files.
        for index, entry in enumerate(entries):
            with content_item(['payload', field, index], entry):
                image_id = str(entry.get("id") or "")
                if image_id in existing:
                    prepared.append(existing[image_id] | {"caption": str(entry.get("caption") or "")})
                else:
                    source = _as_path(entry.get("path"))
                    if not source.is_file() or source.suffix.lower() not in ALLOWED_SUFFIXES:
                        raise ValueError("参考图或图片须为有效的本地图片文件")
                    if source.stat().st_size > MAX_IMAGE_BYTES:
                        raise ValueError("图片超过20MB")
                    prepared.append((source, str(entry.get("caption") or "")))
        return prepared

    def _asset_images(self, db, asset_id, prepared, rollback):
        result = []
        for image in prepared:
            if isinstance(image, dict):
                result.append(image)
                continue
            source, caption = image
            existing_ids = [item["id"] for row in db.execute("SELECT data FROM assets")
                            for field in ("images", "reference_images") for item in json.loads(row[0]).get(field, [])]
            image_id = allocate_id(db, "assets", "asset-image-", existing_ids)
            target = self._asset_root() / asset_id / (image_id + source.suffix.lower())
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output:
                rollback.callback(target.unlink, missing_ok=True)
                on_rollback(self._store.path, lambda path=target: path.unlink(missing_ok=True))
                with source.open('rb') as original:
                    shutil.copyfileobj(original, output)
            result.append({"id": image_id, "file": target.relative_to(self._asset_root()).as_posix(), "caption": caption})
        return result

    def delete_asset(self, asset_id: str, expected_revision=None, force=False) -> dict:
        """删资产。被引用时先列位置；`force` 才连带解除绑定，不静默留下悬空引用。"""
        uses = self.asset_usage(asset_id)
        if uses and not force:
            where = '、'.join(f"{row['unit_id']}/{row['shot_id']}[{row['index']}]" for row in uses[:6])
            raise ValueError(f"资产仍被分镜引用（{where}）；确认要连绑定一起解除时再删")
        cleared = []
        if uses:
            for row in uses:
                if row.get('kind') == 'object':
                    self._unbind_storyboard_object(row['unit_id'], row['object_id'], asset_id)
                else:
                    self._unbind_storyboard_element(row['unit_id'], row['shot_id'], row['index'], asset_id)
                cleared.append({'unit_id': row['unit_id'], 'shot_id': row.get('shot_id'),
                                'index': row.get('index'), 'object_id': row.get('object_id')})
        with self._store.connect(write=True) as project_db, self._asset_db(write=True) as db:
            for row in project_db.execute("SELECT value FROM meta WHERE key LIKE 'storyboard:%'"):
                board = json.loads(row[0])
                references = [*board.get('objects', []), *[element for shot in board.get('shots', []) for element in shot.get('visual_elements', [])]]
                if any(element.get('asset_id') == asset_id for element in references):
                    raise ValueError('资产仍被分镜引用，请先解除绑定')
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            if expected_revision is not None and revision != expected_revision:
                raise ContentConflictError("资产已改变，请刷新后重试")
            if not db.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone():
                raise ContentGuardError("资产不存在")
            db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            revision += 1
            db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
            db.execute("INSERT INTO audit(revision,action,asset_id,summary) VALUES(?,?,?,?)",
                       (revision, "delete", asset_id, "删除资产设定"))
        return {"removed": asset_id, "revision": revision, "cleared_bindings": cleared}

    def _unbind_storyboard_element(self, unit_id, shot_id, index, asset_id):
        board = self.read_storyboard(unit_id)
        shot = next((row for row in board['shots'] if row['id'] == shot_id), None)
        if shot is None or index >= len(shot.get('visual_elements') or []):
            raise ContentGuardError('绑定位置已变化，请重新读取后重试')
        rows = [dict(element) for element in shot['visual_elements']]
        if rows[index].get('asset_id') != asset_id:
            raise ContentConflictError('绑定已变化，请重新读取后重试')
        rows[index]['asset_id'] = None
        self.apply_storyboard_changes(unit_id, board['revision'], [{'op': 'update_shot', 'shot_id': shot_id, 'visual_elements': rows}])

    def _unbind_storyboard_object(self, unit_id, object_id, asset_id):
        board = self.read_storyboard(unit_id)
        self.apply_storyboard_changes(unit_id, board['revision'], [{'op': 'update_object', 'object_id': object_id, 'asset_id': None}])

