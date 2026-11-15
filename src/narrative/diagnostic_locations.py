"""Lossless diagnostic locations shared by Operations submission adapters.

Paths are relative to the published operation input. An empty path explicitly
means the operation as a whole; it must never imply a guessed repair field.
"""
from __future__ import annotations

import json
from copy import deepcopy


def path_text(path):
    """Display JSON tokens without confusing literal dots or brackets with paths."""
    return '$' + ''.join(f'[{token}]' if type(token) is int else
        '[' + json.dumps(token, ensure_ascii=False) + ']' for token in path)


def normalize_problem(problem, *, operation=None, target=None):
    """Preserve producer metadata and explicitly identify a field or whole input."""
    row = deepcopy(problem)
    path = row.get('path')
    if not isinstance(path, (tuple, list)) or any(type(t) not in (str, int) for t in path):
        path = []
    row['path'] = list(path)
    row['message'] = str(row.get('message') or row.get('reason') or row.get('code') or '内容未通过校验')
    row.setdefault('reason', row['message'])
    row.setdefault('severity', 'error')
    row.setdefault('code', 'CONTENT_VALIDATION')
    row['location'] = path_text(row['path'])
    row['location_scope'] = 'field' if row['path'] else 'operation'
    if not row['path']:
        row.setdefault('location_note', '此检查针对整个提交；未确定更细字段，不代表任意字段均可修改')
    if operation is not None:
        row.setdefault('operation', operation)
    if target is not None:
        row.setdefault('target', deepcopy(target))
    return row


def normalize_problems(problems, *, operation=None, target=None):
    return [normalize_problem(row, operation=operation, target=target) for row in problems]


COMPACT_GROUP_LIMIT = 12
COMPACT_INDEX_LIMIT = 12


def compact_problems(problems, *, group_limit: int = COMPACT_GROUP_LIMIT,
                     index_limit: int = COMPACT_INDEX_LIMIT):
    """同类问题折叠成一行：只说一次，附 count 与实际处数涉及的索引。

    为什么放在这里：草稿预检（`extraction_draft._compact`）与暂存入库
    （`extraction_staging._compact`）都要用同一套口径——否则一处折叠了、另一处还是几十行。
    """
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in problems:
        targets = row.get('targets') or [{}]
        target = targets[0] if isinstance(targets[0], dict) else {}
        key = (row.get('severity'), row.get('code'), row.get('message'),
               target.get('section'), target.get('field'))
        entry = groups.get(key)
        if entry is None:
            entry = {'row': deepcopy(row), 'count': 0, 'indexes': [], 'references': [],
                     'targets': [deepcopy(item) for item in targets]}
            groups[key] = entry
            order.append(key)
        entry['count'] += 1
        for item in targets:
            if isinstance(item, dict) and item not in entry['targets']:
                entry['targets'].append(deepcopy(item))
        index = target.get('index')
        if type(index) is int and index not in entry['indexes']:
            entry['indexes'].append(index)
        reference = target.get('reference')
        if isinstance(reference, str) and reference not in entry['references']:
            entry['references'].append(reference)
    compacted = []
    for key in order:
        entry = groups[key]
        row = entry['row']
        produced = row.get('count')
        row['count'] = max(entry['count'], produced) if type(produced) is int else entry['count']
        if entry['indexes']:
            row['indexes'] = entry['indexes'][:index_limit]
            if entry['count'] > 1:
                row['message'] = f"{row['message']}（同一字段共 {entry['count']} 处）"
        if entry['targets']:
            row['targets'] = entry['targets'][:index_limit]
        if entry['references']:
            row['references'] = entry['references'][:index_limit]
        compacted.append(row)
    if len(compacted) > group_limit:
        omitted = len(compacted) - group_limit
        compacted = compacted[:group_limit]
        compacted.append({'severity': 'error', 'code': 'MORE_PROBLEMS', 'path': [], 'targets': [],
                          'message': f"另有 {omitted} 类问题未展开（本次共 {len(problems)} 处）；"
                                     "先修已列出的，再重新提交",
                          'location': '$', 'location_scope': 'operation'})
    return compacted
