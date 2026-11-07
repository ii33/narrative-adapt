"""Shot-local asset analysis and read-only asset element presentation."""
from copy import deepcopy

from .content_errors import (
    ContentConflictError,
    ContentGuardError,
    ContentValidationError,
    content_write,
)

KINDS = {'person': 'appearance', 'prop': 'item', 'set': 'location'}


def element_views(board, lookup):
    result = deepcopy(board)
    for index, shot in enumerate(result['shots']):
        rows, diagnostics = [], []
        for number, element in enumerate(shot.get('visual_elements', [])):
            row = dict(element, asset_title='', category='', label=element.get('name', ''), status='unbound', message='未绑定资产对象')
            identifier = element.get('asset_id')
            if identifier:
                try:
                    card = lookup(identifier)
                except ContentGuardError:
                    row.update(status='missing', message='绑定的资产对象不存在')
                else:
                    title = card['title']
                    label = element['name'] + ' - ' + title if element['kind'] == 'person' and title != element['name'] else title
                    row.update(asset_title=title, category=card['category'], label=label)
                    if card['category'] != KINDS.get(element['kind']):
                        row.update(status='type_mismatch', message='元素类型与资产类别不符')
                    elif not card.get('images') and not card.get('reference_images'):
                        row.update(status='image_missing', message='资产已建立，待添加图片')
                    else:
                        row.update(status='ready', message='')
            if row['status'] != 'ready':
                diagnostics.append({'code': 'ASSET_ELEMENT_' + row['status'].upper(), 'severity': 'warning' if row['status'] != 'image_missing' else 'info',
                                    'message': row['message'], 'path': ['shots', index, 'visual_elements', number, 'asset_id'], 'shot_id': shot['id']})
            rows.append(row)
        shot['asset_elements'] = rows
        shot['asset_elements_status'] = ('issues' if diagnostics else 'complete') if rows else ('empty' if shot.get('assets_analyzed') else 'unanalysed')
        shot['asset_elements_diagnostics'] = diagnostics
        shot['asset_elements_missing_count'] = sum(row['status'] in {'unbound', 'missing', 'type_mismatch'} for row in rows)
        shot['asset_elements_image_pending_count'] = sum(row['status'] == 'image_missing' for row in rows)
    return result


def validate_elements(rows, lookup, *, path, required=False):
    if not isinstance(rows, list):
        raise ContentValidationError('资产元素须为列表', problems=[{'message': '资产元素须为列表', 'path': path}])
    for i, row in enumerate(rows):
        base = [*path, i]
        if not isinstance(row, dict) or set(row) != {'kind', 'name', 'usage', 'asset_id'} or row.get('kind') not in KINDS or not isinstance(row.get('name'), str) or not row['name'].strip() or not isinstance(row.get('usage'), str):
            raise ContentValidationError('元素须包含类型、名称、使用说明和资产编号', problems=[{'message': '元素须包含类型、名称、使用说明和资产编号', 'path': base}])
        identifier = row['asset_id']
        message = ''
        if not identifier:
            if required or identifier is not None:
                message = '每个分析元素必须绑定资产管理中的实际对象；请先保存对应资产'
        elif not isinstance(identifier, str):
            message = '资产编号须为文本'
        else:
            try:
                card = lookup(identifier)
            except (ContentGuardError, KeyError):
                message = '绑定的资产对象不存在'
            else:
                if card['category'] != KINDS[row['kind']]:
                    message = '元素类型与资产类别不符：人物须绑定具体形象，物品绑定物品，场景绑定地点'
        if message:
            raise ContentValidationError(message, problems=[{'code': 'ASSET_ELEMENT_INVALID', 'message': message, 'path': [*base, 'asset_id']}])


class StoryboardAssetsMixin:
    def read_storyboard_assets_context(self, unit_id, shot_ids):
        board = self.read_storyboard(unit_id)
        selected = self._asset_analysis_targets(board, shot_ids)
        context = self.read_script_unit_context(unit_id)
        with self._asset_db() as db:
            import json
            cards = [self._asset_card(json.loads(row[0])) for row in db.execute('SELECT data FROM assets ORDER BY rowid')]
            revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
        return {'unit_id': unit_id, 'revision': board['revision'], 'shot_ids': list(shot_ids), 'shots': selected,
                'confirmed_elements': [{'shot_id': shot['id'], 'visual_elements': [deepcopy(row) for row in shot.get('visual_elements', []) if row.get('asset_id')]} for shot in selected],
                'source_blocks': context['unit']['blocks'], 'assets': cards, 'asset_revision': revision, 'expected_body_revision': context['unit']['body_revision'],
                'creative_agreement': context.get('creative_agreement'),
                'instructions': '只分析尚未确定的出场元素，不创建、修改或自动绑定资产。confirmed_elements为用户已确定对象，保留且不重复列入。用submit_storyboard_assets提交所有所选镜头，visual_elements每项仅含kind/name/usage，程序生成待确定状态；确无新增需求可为空列表。资产由用户在元素列表手动选择或建立。'}

    @staticmethod
    def _asset_analysis_targets(board, shot_ids):
        if not isinstance(shot_ids, list) or not shot_ids or any(not isinstance(v, str) for v in shot_ids) or len(set(shot_ids)) != len(shot_ids):
            raise ContentGuardError('分析目标须为非空且无重复的镜头编号列表')
        indexed = {s['id']: s for s in board['shots']}
        if any(identifier not in indexed for identifier in shot_ids):
            raise ContentGuardError('分析目标包含不属于本场的镜头')
        return [indexed[identifier] for identifier in shot_ids]

    @content_write
    def submit_storyboard_assets(self, unit_id, shot_ids, revision, expected_body_revision, elements):
        board = self.read_storyboard(unit_id)
        self._asset_analysis_targets(board, shot_ids)
        if board.get('approved'):
            raise ContentGuardError('请先解除定案再编辑镜头元素')
        if type(revision) is not int or revision != board['revision']:
            raise ContentConflictError('分镜修订已变化，请重读分析上下文')
        current_body = self.read_script_unit_context(unit_id)['unit']['body_revision']
        if type(expected_body_revision) is not int or expected_body_revision != current_body:
            raise ContentConflictError('正文依据已变化，请重新分析')
        if not isinstance(elements, list):
            raise ContentValidationError('分析结果须为列表', problems=[{'message': '分析结果须为列表', 'path': ['elements']}])
        seen, changes = set(), []
        for i, item in enumerate(elements):
            if not isinstance(item, dict) or set(item) != {'shot_id', 'visual_elements'}:
                raise ContentValidationError('每项须含shot_id及visual_elements', problems=[{'message': '每项须含shot_id及visual_elements', 'path': ['elements', i]}])
            identifier = item['shot_id']
            if not isinstance(identifier, str) or identifier not in shot_ids or identifier in seen:
                raise ContentValidationError('镜头须属于固定选择且只提交一次', problems=[{'message': '镜头须属于固定选择且只提交一次', 'path': ['elements', i, 'shot_id']}])
            seen.add(identifier)
            rows = item['visual_elements']
            if not isinstance(rows, list):
                raise ContentValidationError('元素需求须为列表', problems=[{'message': '元素需求须为列表', 'path': ['elements', i, 'visual_elements']}])
            pending = []
            for number, value in enumerate(rows):
                if not isinstance(value, dict) or set(value) != {'kind', 'name', 'usage'} or value.get('kind') not in KINDS or not isinstance(value.get('name'), str) or not value['name'].strip() or not isinstance(value.get('usage'), str):
                    raise ContentValidationError('分析元素仅包含kind/name/usage，不选择或建立资产', problems=[{'message': '分析元素仅包含kind/name/usage，不选择或建立资产', 'path': ['elements', i, 'visual_elements', number]}])
                pending.append({**deepcopy(value), 'asset_id': None})
            previous = next(shot for shot in board['shots'] if shot['id'] == identifier)
            confirmed = [deepcopy(row) for row in previous.get('visual_elements', []) if row.get('asset_id')]
            changes.append({'op': 'update_shot', 'shot_id': identifier, 'visual_elements': [*confirmed, *pending]})
        if seen != set(shot_ids):
            raise ContentValidationError('分析结果缺少所选镜头：' + '、'.join(v for v in shot_ids if v not in seen), problems=[{'message': '分析结果必须覆盖全部选中镜头', 'path': ['elements']}])
        return self._write_storyboard(unit_id, revision, changes, asset_analysis=True, expected_body_revision=expected_body_revision)


    def create_missing_storyboard_assets(self, unit_id, shot_ids, revision, expected_asset_revision=None, element_refs=None):
        """Explicit atomic command: one independent empty card per unbound selected row."""
        from .asset_operations import AssetOperationsMixin
        from .graph_store import atomic_write_scope

        with atomic_write_scope(self._store):
            board = self.read_storyboard(unit_id)
            selected = self._asset_analysis_targets(board, shot_ids)
            if type(revision) is not int or revision != board['revision']:
                raise ContentConflictError('分镜已改变，请刷新后再建立资产')
            if board.get('approved'):
                raise ContentGuardError('请先解除定案再建立并关联资产')
            selected_by_id = {shot['id']: shot for shot in selected}
            refs = []
            if element_refs is None:
                refs = [(shot['id'], index) for shot in selected for index, _ in enumerate(shot.get('visual_elements', []))]
            else:
                if not isinstance(element_refs, list) or not element_refs:
                    raise ContentGuardError('元素定位须为非空列表')
                for reference in element_refs:
                    if not isinstance(reference, dict) or set(reference) != {'shot_id', 'index'}:
                        raise ContentGuardError('元素定位须包含shot_id和index')
                    owner, index = reference['shot_id'], reference['index']
                    if not isinstance(owner, str) or owner not in selected_by_id or type(index) is not int or not 0 <= index < len(selected_by_id[owner].get('visual_elements', [])):
                        raise ContentGuardError('元素定位不属于所选镜头或已经变化')
                    if (owner, index) in refs:
                        raise ContentGuardError('元素定位不能重复')
                    refs.append((owner, index))
            created, bindings, skipped, changes = [], [], [], []
            # The attached asset database shares the project transaction and rollback.
            with self._asset_db(write=True) as asset_db:
                asset_revision = asset_db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
                if expected_asset_revision is not None and (type(expected_asset_revision) is not int or expected_asset_revision != asset_revision):
                    raise ContentConflictError('资产目录已改变，请刷新后重试')
                updated = {}
                for owner, index in refs:
                    element = selected_by_id[owner]['visual_elements'][index]
                    if element.get('asset_id'):
                        view = selected_by_id[owner].get('asset_elements', [])[index]
                        skipped.append({'shot_id': owner, 'index': index, 'asset_id': element['asset_id'],
                                        'status': view.get('status', 'bound'),
                                        'message': '已有绑定，未自动替换；缺失对象或类型问题请手动处理' if view.get('status') in {'missing', 'type_mismatch'} else '已有绑定，保留原对象'})
                        continue
                    validate_elements([element], self.get_asset, path=['shots', next(i for i, shot in enumerate(board['shots']) if shot['id'] == owner), 'visual_elements'])
                    number = next(i for i, shot in enumerate(board['shots'], 1) if shot['id'] == owner)
                    suffix = '形象' if element['kind'] == 'person' else '资产'
                    title = f"{element['name']} - 镜头{number}元素{index + 1}{suffix}"
                    card = AssetOperationsMixin.save_asset(self, {'category': KINDS[element['kind']], 'title': title,
                        'domain': 'adaptation'}, expected_revision=asset_revision)
                    asset_revision = card['revision']
                    rows = updated.setdefault(owner, deepcopy(selected_by_id[owner]['visual_elements']))
                    rows[index]['asset_id'] = card['id']
                    created.append(card)
                    bindings.append({'shot_id': owner, 'index': index, 'asset_id': card['id'], 'title': card['title']})
                for owner, rows in updated.items():
                    changes.append({'op': 'update_shot', 'shot_id': owner, 'visual_elements': rows})
                if changes:
                    self._write_storyboard(unit_id, revision, changes)
            committed = [{'domain': 'assets', 'action': 'created', 'object_type': 'asset', 'id': card['id'], 'name': card['title'], 'count': 1} for card in created]
            if created:
                committed.append({'domain': 'storyboard', 'action': 'saved', 'object_type': 'storyboard', 'id': unit_id, 'name': unit_id, 'count': 1})
            return {'status': 'accepted' if created else 'unchanged', 'committed_changes': committed,
                    'unit_id': unit_id, 'created_count': len(created), 'created_assets': created, 'bindings': bindings,
                    'skipped': skipped, 'asset_revision': asset_revision, 'board': self.read_storyboard(unit_id),
                    'reuse_policy': '每条未确定元素独立建立一张空卡，不按同名推断复用；可手动选择已有资产复用'}
