"""Read actual persisted task outcomes for the optional event-usage display."""
from __future__ import annotations

import json
from functools import lru_cache

BODY_TOOLS = {'submit_script_unit', 'apply_script_block_changes'}


@lru_cache(maxsize=128)
def _read_task(path, modified, size):
    del modified, size  # Part of the cache identity; task files are updated in place.
    try:
        with open(path, encoding='utf-8') as stream:
            data = json.load(stream)
        if (not isinstance(data, dict) or not isinstance(data.get('status'), str)
                or 'writing_task' in data and not isinstance(data['writing_task'], dict)
                or 'steps' in data and not isinstance(data['steps'], list)):
            return None
        return data
    except (OSError, ValueError):
        return None


def generation_status(task_root, state, screenplay_id):
    """A task must really finish with a body write; a scene table alone is not enough."""
    units = state.get('script_units', {})
    segments = state.get('script_segments', {})

    def belongs(identifier):
        segment = segments.get(units.get(identifier, {}).get('segment_id'), {})
        return segment.get('screenplay_id') == screenplay_id

    def written_units(audit):
        submitted = audit.get('submitted_unit_ids')
        found = {i for i in submitted if isinstance(i, str)} if isinstance(submitted, list) else set()
        for step in audit.get('steps') or []:
            if not isinstance(step, dict):
                continue
            result = step.get('result') if isinstance(step.get('result'), dict) else {}
            operation = result.get('operation') or step.get('tool')
            if (step.get('ok') is not True or operation not in BODY_TOOLS
                    or result.get('status') not in {'accepted', 'unchanged'}):
                continue
            args = step.get('args') if isinstance(step.get('args'), dict) else {}
            result_target = result.get('target') if isinstance(result.get('target'), dict) else {}
            identifier = args.get('unit_id') or result_target.get('unit_id') or result.get('id')
            if isinstance(identifier, str):
                found.add(identifier)
        return {identifier for identifier in found if belongs(identifier)}

    try:
        paths = sorted(task_root.glob('*/task.json'), key=lambda path: path.parent.name, reverse=True)
    except OSError:
        return {'status': 'unknown', 'generation_state_reason': '任务记录不可读取'}
    for path in paths:
        try:
            stamp = path.stat()
        except OSError:
            return {'status': 'unknown', 'generation_state_reason': '任务记录不可读取'}
        audit = _read_task(str(path), stamp.st_mtime_ns, stamp.st_size)
        if audit is None:
            return {'status': 'unknown', 'generation_state_reason': '任务记录未完成或不可读取'}
        task = audit.get('writing_task') or {}
        target = task.get('target') if isinstance(task.get('target'), dict) else {}
        declared = target.get('screenplay_id') or task.get('screenplay_id')
        targets_this_script = declared == screenplay_id
        for identifier in target.get('ids', []) if isinstance(target.get('ids'), list) else []:
            if not isinstance(identifier, str):
                continue
            kind = target.get('kind')
            owner = (units.get(identifier, {}).get('segment_id') if kind == 'unit' else identifier)
            if kind in {'unit', 'segment', 'scene_table'}:
                targets_this_script |= segments.get(owner, {}).get('screenplay_id') == screenplay_id
            elif kind == 'screenplay':
                targets_this_script |= identifier == screenplay_id
        written = written_units(audit)
        related = targets_this_script or bool(written)
        status = audit.get('status', 'unknown')
        if not related:
            # A normal chat may write screenplay content without a page target.
            # Until its tool results are saved, its destination is unconfirmed.
            if not task and status in {'running', 'starting'}:
                return {'status': 'running', 'generation_state_reason': '当前助手任务尚未结束'}
            continue
        if status != 'completed':
            return {'status': status, 'generation_state_reason': '本剧本最近任务尚未成功结束'}
        has_body = any(block.get('unit_id') in written for block in state.get('script_blocks', {}).values())
        return {'status': 'completed' if has_body else 'not_generated',
                'generation_state_reason': '任务已成功结束并保存正文' if has_body else '本任务没有保存正文'}
    return {'status': 'not_generated', 'generation_state_reason': '尚无已完成的正文生成任务'}


def attach_generation_status(ops, state, screenplay_id, usage):
    from .project_package import layout
    root = layout(ops.project_info().path).extraction / 'assistant' / 'tasks'
    return dict(usage) | generation_status(root, state, screenplay_id)
