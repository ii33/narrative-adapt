"""Thin host-permission, audit and paging adapters for the single text backend."""
from __future__ import annotations

import hashlib

from . import host_text


def _encoding(value):
    return None if value == 'auto' else value


def _public_metadata(result):
    return {key: value for key, value in result.items() if key not in {'text', 'expected_bytes_sha256'}}


def _error(ops, bound, operation, args, error):
    from .assistant_host import _audit, _redact
    message = str(error)
    if isinstance(error, host_text.TextEncodingError) and error.candidates:
        message += '；候选编码：' + '、'.join(error.candidates)
    message = _redact(message, bound.secrets)
    _audit(ops, bound, operation, args, error=message)
    raise ValueError(message) from None


def read_text_file(ops, path, offset=0, limit=16000, encoding='auto'):
    from .assistant_host import _absolute, _audit, _guard, _masked_page, _page
    bound = _guard(ops)
    args = {'path': path, 'offset': offset, 'limit': limit, 'encoding': encoding}
    try:
        _page(offset, limit)
        target = _absolute(path)
        decoded = host_text.read_text(target, encoding=_encoding(encoding))
        text = decoded['text']
        count = len(text[offset:offset + limit])
        content, redacted = _masked_page(text, offset, count, bound.secrets)
        result = _public_metadata(decoded) | {'path': str(target), 'content': content,
            'encoding': decoded['codec'], 'offset': offset, 'offset_unit': 'characters',
            'next_offset': offset + count, 'eof': offset + count >= len(text), 'redacted': redacted}
    except Exception as error:
        return _error(ops, bound, 'read_host_file', args, error)
    return _audit(ops, bound, 'read_host_file', args, result)


def write_text_file(ops, path, content, encoding='auto', append=False, eol=None):
    from .assistant_host import _absolute, _audit, _guard
    bound = _guard(ops)
    args = {'path': path, 'content': content, 'encoding': encoding, 'append': append, 'eol': eol}
    try:
        if not isinstance(content, str) or type(append) is not bool:
            raise ValueError('文件内容须为文本，append须为布尔值')
        target = _absolute(path)
        before, previous = 0, None
        text = content
        if target.exists():
            previous = host_text.read_text(target, encoding=_encoding(encoding))
            before = previous['byte_length']
            if append:
                text = previous['text'] + content
        _guard(ops)
        metadata = host_text.write_text(target, text, encoding=_encoding(encoding), eol=eol,
            expected_content=previous['text'] if previous else '',
            expected_bytes_sha256=previous['expected_bytes_sha256'] if previous else hashlib.sha256(b'').hexdigest())
        result = _public_metadata(metadata) | {'path': str(target), 'append': append,
            'bytes_written': metadata['byte_length'] - before if append else metadata['byte_length']}
    except Exception as error:
        return _error(ops, bound, 'write_host_file', args, error)
    return _audit(ops, bound, 'write_host_file', args, result)


def edit_text_file(ops, path, old_text, new_text, replace_all=False, encoding='auto'):
    from .assistant_host import _absolute, _audit, _guard
    bound = _guard(ops)
    args = {'path': path, 'old_text': old_text, 'new_text': new_text,
        'replace_all': replace_all, 'encoding': encoding}
    try:
        if type(replace_all) is not bool:
            raise ValueError('replace_all须为布尔值')
        target = _absolute(path)
        metadata = host_text.edit_text(target, old_text, new_text,
            replace_all=replace_all, encoding=_encoding(encoding))
        result = _public_metadata(metadata) | {'path': str(target), 'bytes_written': metadata['byte_length']}
    except Exception as error:
        return _error(ops, bound, 'edit_host_file', args, error)
    return _audit(ops, bound, 'edit_host_file', args, result)
