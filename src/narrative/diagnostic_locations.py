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
