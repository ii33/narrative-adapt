"""Strict, bounded local text IO; independent of business data and host permissions."""
from __future__ import annotations

import base64
import codecs
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
_HELPER = Path(__file__).parent / 'vendor' / 'dsh_agentkit' / 'encode_write.py'
_BOMS = {'utf-8': codecs.BOM_UTF8, 'utf-16le': codecs.BOM_UTF16_LE, 'utf-16be': codecs.BOM_UTF16_BE}
_CODECS = {'utf-8': 'utf-8', 'utf-16le': 'utf-16-le', 'utf-16be': 'utf-16-be',
    'gbk': 'gbk', 'gb18030': 'gb18030', 'big5': 'big5'}
_EOLS = {'lf': '\n', 'crlf': '\r\n', 'cr': '\r'}
_LOCK = threading.RLock()


class TextEncodingError(ValueError):
    def __init__(self, message, *, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


class TextConflictError(ValueError):
    pass


def _codec(encoding):
    aliases = {'utf8': 'utf-8', 'utf-8-sig': 'utf-8', 'utf-16-le': 'utf-16le', 'utf-16-be': 'utf-16be'}
    name = aliases.get(str(encoding).lower(), str(encoding).lower())
    if name not in _CODECS:
        raise TextEncodingError('不支持的文本编码')
    return name


def _read_bytes(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('文本文件超过8MiB，不能截断后编辑或写回')
    return raw


def _decode(raw, encoding=None):
    bom_codec = next((name for name, bom in _BOMS.items() if raw.startswith(bom)), None)
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise TextEncodingError('UTF-32不在支持范围')
    if encoding is not None:
        chosen = _codec(encoding)
        if bom_codec and chosen != bom_codec:
            raise TextEncodingError('指定编码与文件BOM冲突')
        payload = raw[len(_BOMS[chosen]):] if bom_codec else raw
        try:
            text = payload.decode(_CODECS[chosen], errors='strict')
        except UnicodeDecodeError as exc:
            raise TextEncodingError('文件不能按指定编码严格解码') from exc
        return text, chosen, bool(bom_codec), [chosen]
    if bom_codec:
        return _decode(raw, bom_codec)
    if b'\x00' in raw:
        raise TextEncodingError('文件含NUL，可能是二进制或无BOM的UTF-16；请显式指定编码')
    try:
        return raw.decode('utf-8', errors='strict'), 'utf-8', False, ['utf-8']
    except UnicodeDecodeError:
        pass
    candidates = []
    for name in ('gb18030', 'big5'):
        try:
            text = raw.decode(_CODECS[name], errors='strict')
            if text.encode(_CODECS[name], errors='strict') == raw:
                candidates.append((name, text))
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    if not candidates:
        raise TextEncodingError('无法严格解码为支持的文本编码；不会使用有损替换')
    if len({text for _, text in candidates}) > 1:
        raise TextEncodingError('旧编码判定有歧义，请显式指定编码', candidates=[name for name, _ in candidates])
    return candidates[0][1], candidates[0][0], False, [name for name, _ in candidates]


def _normal(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _metadata(text, codec, bom, raw, candidates):
    separators = re.findall(r'\r\n|\r|\n', text)
    counts = {name: separators.count(value) for name, value in _EOLS.items()}
    used = [name for name, count in counts.items() if count]
    return {'codec': codec, 'bom': bom, 'eol': used[0] if len(used) == 1 else 'mixed' if used else 'none',
        'eol_counts': counts, 'byte_length': len(raw), 'ambiguous': False, 'candidates': candidates,
        'expected_bytes_sha256': hashlib.sha256(raw).hexdigest()}


def read_text(path, *, encoding=None):
    raw = _read_bytes(Path(path))
    return decode_bytes(raw, encoding=encoding)


def decode_bytes(data, *, encoding=None):
    """Decode an entire supplied buffer; the caller owns any stream-size policy."""
    if not isinstance(data, bytes):
        raise TypeError('待解码内容须为bytes')
    text, codec, bom, candidates = _decode(data, encoding)
    return {'text': text, **_metadata(text, codec, bom, data, candidates)}


def _render(text, previous, eol):
    normalized = _normal(text)
    if eol is not None:
        if eol not in _EOLS:
            raise ValueError('换行格式须为lf/crlf/cr')
        return normalized.replace('\n', _EOLS[eol])
    old_separators = re.findall(r'\r\n|\r|\n', previous)
    kinds = set(old_separators)
    if len(kinds) <= 1:
        return normalized.replace('\n', old_separators[0] if old_separators else '\n')
    parts = normalized.split('\n')
    if len(parts) - 1 != len(old_separators):
        raise ValueError('混合换行文件的行数改变，无法保留全部原换行；请显式指定eol')
    return ''.join(part + (old_separators[i] if i < len(old_separators) else '') for i, part in enumerate(parts))


def _encode(text, codec, bom):
    if len(text.encode('utf-8', errors='strict')) > MAX_BYTES * 4:
        raise ValueError('待写文本过大')
    result = subprocess.run([sys.executable, str(_HELPER), '--encode-only', codec, '1' if bom else '0'],
        input=text.encode('utf-8', errors='strict'), capture_output=True, shell=False, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise TextEncodingError('文本无法以原编码严格写回，原文件未修改')
    raw = base64.b64decode(result.stdout, validate=True)
    if len(raw) > MAX_BYTES:
        raise ValueError('编码后文本超过8MiB，原文件未修改')
    return raw


def _check_expected(original, decoded, expected_content, expected_bytes_sha256):
    if expected_content is not None and decoded != expected_content:
        raise TextConflictError('文件内容已改变，请重新读取后修改')
    if expected_bytes_sha256 is not None and hashlib.sha256(original or b'').hexdigest() != expected_bytes_sha256:
        raise TextConflictError('文件字节已改变，请重新读取后修改')


def _commit(path, payload, original):
    descriptor, temporary = tempfile.mkstemp(prefix='.narrative-text-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if original is None:
            # Hard-link creation fails if another writer created this name.
            os.link(temporary, path)
        else:
            if _read_bytes(path) != original:
                raise TextConflictError('编码期间文件已被其他写入修改，原文件未覆盖')
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_text(path, text, *, encoding=None, eol=None, expected_content=None, expected_bytes_sha256=None):
    if not isinstance(text, str):
        raise TypeError('文本内容须为字符串')
    path = Path(path).resolve()
    with _LOCK:
        original = _read_bytes(path) if path.exists() else None
        if original is None:
            previous, codec, bom = '', _codec(encoding or 'utf-8'), encoding == 'utf-8-sig'
        else:
            previous, codec, bom, _ = _decode(original, encoding)
        _check_expected(original, previous, expected_content, expected_bytes_sha256)
        rendered = _render(text, previous, eol)
        payload = _encode(rendered, codec, bom)
        _commit(path, payload, original)
        return _metadata(rendered, codec, bom, payload, [codec])


def edit_text(path, old_text, new_text, *, replace_all=False, encoding=None, expected_content=None, expected_bytes_sha256=None):
    if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
        raise ValueError('old_text须为非空文本，new_text须为文本')
    path = Path(path).resolve()
    with _LOCK:
        original = _read_bytes(path)
        previous, codec, bom, _ = _decode(original, encoding)
        _check_expected(original, previous, expected_content, expected_bytes_sha256)
        normalized, old, new = _normal(previous), _normal(old_text), _normal(new_text)
        occurrences, offset = [], 0
        while (offset := normalized.find(old, offset)) >= 0:
            occurrences.append(offset)
            offset += 1
        count = len(occurrences)
        if not count or (count > 1 and not replace_all):
            raise ValueError('未找到精确文本' if not count else '匹配不唯一，请明确replace_all或提供更长文本')
        if any(right < left + len(old) for left, right in zip(occurrences, occurrences[1:], strict=False)):
            raise ValueError('匹配范围互相重叠，请提供更明确的文本')
        # Map normalized character boundaries back to the untouched original.
        boundaries, index = [0], 0
        while index < len(previous):
            index += 2 if previous.startswith('\r\n', index) else 1
            boundaries.append(index)
        positions = [(boundaries[start], boundaries[start + len(old)]) for start in occurrences]
        rendered = previous
        for start, end in reversed(positions):
            region = previous[start:end]
            separators = re.findall(r'\r\n|\r|\n', region) or re.findall(r'\r\n|\r|\n', previous)
            separator = max(dict.fromkeys(separators), key=separators.count) if separators else '\n'
            replacement = new.replace('\n', separator)
            if len(set(separators)) > 1 and new.count('\n') == len(re.findall(r'\r\n|\r|\n', region)):
                replacement = _render(new, region, None)
            rendered = rendered[:start] + replacement + rendered[end:]
        payload = _encode(rendered, codec, bom)
        _commit(path, payload, original)
        return _metadata(rendered, codec, bom, payload, [codec]) | {'replacements': count}
