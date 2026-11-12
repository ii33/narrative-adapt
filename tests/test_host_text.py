"""Real local byte roundtrips and conflict failures; no shell or model services."""
import codecs

import pytest

from narrative import host_text

pytestmark = pytest.mark.slow


@pytest.mark.parametrize(('codec', 'bom'), [('utf-8', False), ('utf-8', True),
    ('utf-16le', True), ('utf-16be', True), ('utf-16le', False), ('utf-16be', False), ('gbk', False), ('big5', False)])
@pytest.mark.parametrize('eol', ['\n', '\r\n', '\r'])
def test_existing_codec_bom_and_eol_survive_edit(tmp_path, codec, bom, eol):
    text = '中文第一行' + eol + '第二行 "引號" \\path'
    python_codec = {'utf-16le': 'utf-16-le', 'utf-16be': 'utf-16-be'}.get(codec, codec)
    prefix = {'utf-8': codecs.BOM_UTF8, 'utf-16le': codecs.BOM_UTF16_LE, 'utf-16be': codecs.BOM_UTF16_BE}.get(codec, b'') if bom else b''
    path = tmp_path / 'existing.txt'
    path.write_bytes(prefix + text.encode(python_codec))
    encoding = codec if not bom and codec != 'utf-8' else None
    read = host_text.read_text(path, encoding=encoding)
    assert read['text'] == text and read['bom'] is bom
    result = host_text.edit_text(path, '第一行\n第二行', '改後\n第二行', encoding=encoding,
        expected_bytes_sha256=read['expected_bytes_sha256'])
    expected = text.replace('第一行', '改後')
    assert path.read_bytes() == prefix + expected.encode(python_codec)
    assert result['replacements'] == 1


def test_new_default_utf8_lf_and_literal_escapes_are_not_interpreted(tmp_path):
    path = tmp_path / 'new.txt'
    text = '引号" \\n \\t 😀\r\n下一行'
    host_text.write_text(path, text)
    assert path.read_bytes() == text.replace('\r\n', '\n').encode()
    host_text.edit_text(path, r'\n', r'\r\n')
    assert r'\r\n' in host_text.read_text(path)['text']


def test_legacy_ambiguity_is_explicit_and_override_uses_same_decoder():
    raw = '中文'.encode('gbk')
    with pytest.raises(host_text.TextEncodingError) as error:
        host_text.decode_bytes(raw)
    assert set(error.value.candidates) == {'gb18030', 'big5'}
    assert host_text.decode_bytes(raw, encoding='gbk')['text'] == '中文'
    with pytest.raises(host_text.TextEncodingError):
        host_text.decode_bytes(b'\xff')


def test_mixed_newlines_preserved_and_whole_write_cannot_silently_normalize(tmp_path):
    path = tmp_path / 'mixed.txt'
    original = 'one\r\ntwo\rthree\nfour'
    path.write_bytes(original.encode())
    assert host_text.read_text(path)['eol'] == 'mixed'
    host_text.edit_text(path, 'two\nthree', 'TWO\nTHREE')
    assert path.read_bytes() == original.replace('two', 'TWO').replace('three', 'THREE').encode()
    host_text.write_text(path, '1\n2\n3\n4')
    assert path.read_bytes() == b'1\r\n2\r3\n4'
    before = path.read_bytes()
    with pytest.raises(ValueError, match='混合换行'):
        host_text.write_text(path, '1\n2')
    assert path.read_bytes() == before


def test_unencodable_input_and_conflict_never_overwrite_original(tmp_path, monkeypatch):
    path = tmp_path / 'gbk.txt'
    original = '原文'.encode('gbk')
    path.write_bytes(original)
    with pytest.raises(host_text.TextEncodingError):
        host_text.write_text(path, '😀', encoding='gbk')
    assert path.read_bytes() == original
    with pytest.raises(host_text.TextConflictError):
        host_text.write_text(path, '新文', encoding='gbk', expected_content='过时内容')
    with pytest.raises(host_text.TextConflictError):
        host_text.write_text(path, '新文', encoding='gbk', expected_bytes_sha256='stale-byte-snapshot')
    assert path.read_bytes() == original
    encode = host_text._encode

    def concurrent_write(*args):
        result = encode(*args)
        path.write_bytes('他人更新'.encode('gbk'))
        return result

    monkeypatch.setattr(host_text, '_encode', concurrent_write)
    with pytest.raises(host_text.TextConflictError):
        host_text.write_text(path, '我的更新', encoding='gbk')
    assert path.read_bytes() == '他人更新'.encode('gbk')
    assert not list(tmp_path.glob('.narrative-text-*'))


def test_unique_matching_replace_all_and_overlaps(tmp_path):
    path = tmp_path / 'matches.txt'
    path.write_text('same same', encoding='utf-8')
    for old in ('missing', 'same'):
        with pytest.raises(ValueError):
            host_text.edit_text(path, old, 'changed')
        assert path.read_text() == 'same same'
    assert host_text.edit_text(path, 'same', 'changed', replace_all=True)['replacements'] == 2
    path.write_text('aaaa', encoding='utf-8')
    with pytest.raises(ValueError):
        host_text.edit_text(path, 'aaa', 'x')


def test_bom_conflict_and_file_limit_have_no_partial_write(tmp_path, monkeypatch):
    path = tmp_path / 'large.txt'
    path.write_bytes(codecs.BOM_UTF16_LE + 'abc'.encode('utf-16-le'))
    with pytest.raises(host_text.TextEncodingError, match='BOM'):
        host_text.read_text(path, encoding='utf-8')
    monkeypatch.setattr(host_text, 'MAX_BYTES', 4)
    with pytest.raises(ValueError, match='8MiB'):
        host_text.read_text(path)
    small = tmp_path / 'small.txt'
    small.write_bytes(b'old')
    with pytest.raises(ValueError, match='8MiB'):
        host_text.write_text(small, '12345')
    assert small.read_bytes() == b'old'
    assert host_text.decode_bytes(b'longer than file limit')['text'] == 'longer than file limit'


def test_encode_helper_is_argv_only_utf8_stdin_and_exact_vendored_copy(tmp_path, monkeypatch):
    run = host_text.subprocess.run
    calls = []

    def observe(argv, **kwargs):
        calls.append((argv, kwargs))
        return run(argv, **kwargs)

    monkeypatch.setattr(host_text.subprocess, 'run', observe)
    host_text.write_text(tmp_path / 'quoted name.txt', 'quote"\\😀')
    argv, options = calls[0]
    assert argv[0] == host_text.sys.executable and argv[2:] == ['--encode-only', 'utf-8', '0']
    assert options['shell'] is False and options['input'] == 'quote"\\😀'.encode()
    assert (host_text._HELPER.parent / 'LICENSE').read_text().startswith('MIT License')
