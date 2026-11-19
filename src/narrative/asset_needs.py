"""分镜资产需求：想要什么（需求）与绑到哪张卡（引用）分开存。

为什么单列一层需求：排镜时先说清「这场／这镜要哪些人物形象、物品、地点、材质」，
真正绑资产往往晚一步，甚至要用户在资产列表里手动挑。需求单独存，就能在
不猜身份、不自动建卡的前提下同时报出两类不同的缺口：

* 需求未落实 —— 写了需求，但没有任何引用把它绑到实际资产卡上；
* 引用悬空 —— ``visual_elements`` 里写了 ``asset_id``，但资产卡不存在或还没有参考图。

两类缺口都是**派生视图**，绝不回写：需求留在 ``board['asset_needs']`` /
``shot['asset_needs']``，引用留在既有的 ``shot['visual_elements']`` 与场平面对象里。

第三类缺口是**布景依赖**：场通过 ``board['set_ref'] = {asset_id, revision}`` 指向一张
地点资产的布景（``asset['set']``），状态同样现算（见 ``unit_set_status``）。
"""
from __future__ import annotations

from copy import deepcopy

from .content_errors import ContentGuardError, ContentValidationError

# 需求用「要什么」的说法；资产卡按 category 归档，两套词不混用。
NEED_KINDS = ('person', 'prop', 'location', 'material')
CATEGORY = {'person': 'appearance', 'prop': 'item', 'location': 'location', 'material': 'material'}
# 场平面对象与视觉元素只分 person/prop/set：材质没有对应的元素类型，只能靠场平面对象绑定。
ELEMENT_KIND = {'person': 'person', 'prop': 'prop', 'location': 'set'}
ELEMENT_TO_NEED = {value: key for key, value in ELEMENT_KIND.items()}
FIELDS = {'kind', 'name', 'variant', 'note'}
_LABEL = 'person（人物形象）/ prop（物品）/ location（地点）/ material（材质）'


def normalize_name(value):
    """同一个对象写「甲」「 甲 」只该算一条需求：比名字前先抹掉空白与大小写差异。"""
    return ' '.join(str(value or '').split()).casefold()


def validate_needs(rows, path=('needs',)):
    """校验并规范化需求列表；同一列表内不重复，空的可选字段直接丢掉。"""
    if not isinstance(rows, list):
        raise ContentValidationError('资产需求须为列表', problems=[{'message': '资产需求须为列表', 'path': list(path)}])
    result, seen = [], set()
    for index, row in enumerate(rows):
        base = [*path, index]
        invalid = (not isinstance(row, dict) or set(row) - FIELDS
                   or not isinstance(row.get('kind'), str) or row['kind'] not in NEED_KINDS
                   or not isinstance(row.get('name'), str) or not row['name'].strip()
                   or any(key in row and not isinstance(row[key], str) for key in ('variant', 'note')))
        if invalid:
            message = f'每条需求须含kind（{_LABEL}）与非空name，可选variant、note文本'
            raise ContentValidationError(message, problems=[{'code': 'ASSET_NEED_INVALID', 'message': message, 'path': base}])
        need = {'kind': row['kind'], 'name': row['name'].strip()}
        for key in ('variant', 'note'):
            text = (row.get(key) or '').strip()
            if text:
                need[key] = text
        key = (need['kind'], normalize_name(need['name']), normalize_name(need.get('variant')))
        if key in seen:
            message = '同一列表里的需求不能重复；同对象同形态只写一条'
            raise ContentValidationError(message, problems=[{'code': 'ASSET_NEED_DUPLICATE', 'message': message, 'path': base}])
        seen.add(key)
        result.append(need)
    return result


def merge_needs(current, extra):
    """追加式声明：已声明的需求顺序不变，只补新条目（同对象同形态不重复加）。"""
    merged = deepcopy(current)
    seen = {(row['kind'], normalize_name(row['name']), normalize_name(row.get('variant'))) for row in merged}
    for row in extra:
        key = (row['kind'], normalize_name(row['name']), normalize_name(row.get('variant')))
        if key not in seen:
            seen.add(key)
            merged.append(row)
    return merged


def references(board):
    """引用池：镜级 visual_elements + 场平面对象；两类都直接指向资产卡编号。"""
    rows = []
    for shot in board.get('shots') or []:
        for index, element in enumerate(shot.get('visual_elements') or []):
            rows.append({'scope': 'shot', 'shot_id': shot['id'], 'index': index,
                         'kind': ELEMENT_TO_NEED.get(element.get('kind')), 'asset_id': element.get('asset_id'),
                         'name': element.get('name', ''), 'variant': element.get('variant', '')})
    for index, obj in enumerate(board.get('objects') or []):
        rows.append({'scope': 'unit', 'shot_id': None, 'index': index,
                     'kind': ELEMENT_TO_NEED.get(obj.get('kind')), 'asset_id': obj.get('asset_id'),
                     'name': obj.get('name', ''), 'variant': ''})
    return rows


def _has_image(card):
    return bool(card.get('images') or card.get('reference_images'))


# 场的布景依赖状态：五种取值，派生不落库（见 ``unit_set_status``）。
# set_missing 三种（unbound/no_set/missing_asset）是要人处理的缺口；stale 只是提示。
SET_STATUS = ('bound', 'stale', 'no_set', 'missing_asset', 'unbound')
SET_MISSING = ('unbound', 'no_set', 'missing_asset')


def set_ref_of(board):
    """场的布景依赖 ``{asset_id, revision}``；形状坏掉时按未绑定处理。

    为什么坏形状不报错：这是派生视图，读盘的历史数据不该因为一个字段缺半截就让整页打不开；
    真要修，缺口视图会把它列为「未绑定」，人看得见。
    """
    ref = board.get('set_ref') if isinstance(board, dict) else None
    if not isinstance(ref, dict):
        return None
    asset_id = ref.get('asset_id')
    if not isinstance(asset_id, str) or not asset_id.strip():
        return None
    revision = ref.get('revision')
    return {'asset_id': asset_id.strip(),
            'revision': revision if isinstance(revision, int) and not isinstance(revision, bool) else None}


def _set_row(card):
    value = card.get('set') if isinstance(card, dict) else None
    return value if isinstance(value, dict) else None


def unit_set_status(board, lookup):
    """场的布景依赖状态：bound/stale/no_set/missing_asset/unbound。

    为什么现算而不落库：``set_ref.revision`` 只说明「这场当时依据哪一版布景」，资产里的
    布景此后可能还被改过；把状态写回库里，两边就各自维护一份会过期的判断。

    ``lookup`` 是资产查询（``get_asset``）；查不到的资产按 missing_asset 报，不当异常。
    """
    ref = set_ref_of(board)
    row = {'unit_id': board.get('unit_id') if isinstance(board, dict) else None,
           'status': 'unbound', 'asset_id': None, 'bound_revision': None, 'revision': None,
           'block_count': 0, 'camera_count': 0, 'room': None, 'asset': None,
           'message': '本场还没有绑定布景资产'}
    if ref is None:
        return row
    row |= {'asset_id': ref['asset_id'], 'bound_revision': ref['revision']}
    try:
        card = lookup(ref['asset_id'])
    except (ContentGuardError, KeyError, ValueError):
        return row | {'status': 'missing_asset',
                      'message': '绑定的布景资产不存在：' + ref['asset_id'] + '；请重新绑定或解除依赖'}
    row['asset'] = {'asset_id': card.get('id') or ref['asset_id'],
                    'title': card.get('title') or '', 'category': card.get('category') or ''}
    data = _set_row(card)
    if card.get('category') != 'location':
        return row | {'status': 'no_set', 'message': '绑定的资产不是地点资产，没有布景'}
    if data is None:
        return row | {'status': 'no_set', 'message': '地点资产已绑定，但还没有布景（用save_location_set写一套）'}
    revision = data.get('revision') if isinstance(data.get('revision'), int) else 0
    blocks = data.get('blocks') or []
    cameras = data.get('cameras') or []
    row |= {'revision': revision, 'block_count': len(blocks), 'camera_count': len(cameras),
            'room': data.get('room')}
    if row['bound_revision'] is not None and row['bound_revision'] < revision:
        return row | {'status': 'stale',
                      'message': '本场依据的是布景第' + str(row['bound_revision']) + '版，资产已到第'
                                 + str(revision) + '版（提示，不是错误）'}
    return row | {'status': 'bound',
                  'message': '布景已绑定：第' + str(revision) + '版，' + str(len(blocks))
                             + '个盒体、' + str(len(cameras)) + '台机位'}


def gap_view(board, lookup):
    """派生缺口视图；两类缺口分开列，计数便于界面直接显示。

    绑定只认同名引用：程序不猜「甲」是不是「甲·老年形象」。需求写了 variant 时，
    引用侧的形态名也必须对得上；没写就按同名取第一条已绑定且类别相符的卡。
    """
    unit_items = validate_needs(board.get('asset_needs') or [], ('asset_needs',))
    shot_items = [(shot, validate_needs(shot.get('asset_needs') or [], ('shots', index, 'asset_needs')))
                  for index, shot in enumerate(board.get('shots') or [])]
    pool = references(board)
    cards = {}

    def card_of(identifier):
        # 一次视图里同一张卡只查一次：get_asset 会顺带算「谁在用它」，别按引用条数重复扫。
        if identifier not in cards:
            try:
                cards[identifier] = lookup(identifier)
            except (ContentGuardError, KeyError, ValueError):
                cards[identifier] = None
        return cards[identifier]

    dangling = []
    for row in pool:
        if row['scope'] != 'shot' or not row.get('asset_id'):
            continue
        card = card_of(row['asset_id'])
        if card is None:
            reason, message = 'missing', '绑定的资产对象不存在'
        elif not _has_image(card):
            reason, message = 'image_missing', '资产已建立，待添加参考图'
        else:
            continue
        dangling.append({'shot_id': row['shot_id'], 'asset_id': row['asset_id'], 'reason': reason,
                         'index': row['index'], 'name': row['name'], 'message': message})

    needs = []

    def resolve(need, *, scope, shot_id):
        # 场级需求可由本场任何引用落实；镜级需求只认本镜元素和场平面对象（对象是全镜共享的）。
        candidates = pool if scope == 'unit' else [
            row for row in pool if row['scope'] == 'unit' or row['shot_id'] == shot_id]
        bound = None
        for row in candidates:
            if not row.get('asset_id') or normalize_name(row['name']) != normalize_name(need['name']):
                continue
            if row.get('kind') and row['kind'] != need['kind']:
                continue
            card = card_of(row['asset_id'])
            if card is None or card.get('category') != CATEGORY[need['kind']]:
                continue
            wanted = normalize_name(need.get('variant'))
            if wanted and normalize_name(row.get('variant')) != wanted:
                continue
            bound = row['asset_id']
            break
        needs.append({'scope': scope, 'shot_id': shot_id, **deepcopy(need), 'bound_asset_id': bound})

    for need in unit_items:
        resolve(need, scope='unit', shot_id=None)
    for shot, rows in shot_items:
        for need in rows:
            resolve(need, scope='shot', shot_id=shot['id'])
    unresolved = sum(1 for row in needs if not row['bound_asset_id'])
    # 布景依赖单列一类缺口：它跟「人物形象缺参考图」不是一回事，混进 dangling 会让人
    # 在资产列表里找一个根本不存在的对象。stale 只作提示——不能因为资产改了布景就拦人写作。
    dependency = unit_set_status(board, card_of)
    return {'unit_id': board.get('unit_id'), 'revision': board.get('revision'), 'needs': needs, 'dangling': dangling,
            'set_dependency': dependency,
            'set_missing': [dependency] if dependency['status'] in SET_MISSING else [],
            'set_stale': [dependency] if dependency['status'] == 'stale' else [],
            'counts': {'unit': len(unit_items), 'shot': sum(len(rows) for _, rows in shot_items),
                       'total': len(needs), 'resolved': len(needs) - unresolved, 'unresolved': unresolved,
                       'dangling': len(dangling)}}
