"""Real local host wrapper permission, audit, charset and paging behavior."""
import codecs
import json
import threading

import pytest

from narrative.assistant_host import host_access_context
from narrative.host_text_operations import edit_text_file, read_text_file, write_text_file
from narrative.operations import Operations
from narrative.project_package import layout


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    return Operations.create_graph_project(tmp_path / 'text.nproj')


def access(ops, mode='full', secrets=()):
    return host_access_context(mode, 'text-task', threading.Event(), ops.project_info().path,
        secrets_to_redact=secrets)


def audits(ops):
    root = layout(ops.project_info().path).extraction / 'assistant/tasks/text-task/host'
    return [json.loads(p.read_text(encoding='utf-8')) for p in root.glob('operation-*.json')]


def test_current_full_permission_is_required(ops, tmp_path):
    path = tmp_path / 'outside.txt'
    with pytest.raises(ValueError, match='完全访问'):
        write_text_file(ops, str(path), 'x')
    with access(ops, 'standard'), pytest.raises(ValueError, match='完全访问'):
        write_text_file(ops, str(path), 'x')
    assert not path.exists()


def test_read_preserves_raw_eol_and_masks_secret_across_page_boundaries(ops, tmp_path):
    path = tmp_path / 'secret.txt'
    path.write_bytes('前缀\r\nlocal-secret-abcdef\r尾巴'.encode())
    with access(ops, secrets=('local-secret-abcdef',)):
        page = read_text_file(ops, str(path), offset=7, limit=3)
        assert page['content'] == '[REDACTED]' and page['next_offset'] == 10
        assert page['eol'] == 'mixed' and page['codec'] == 'utf-8'
        assert page['offset_unit'] == 'characters' and page['redacted']
        assert 'expected_bytes_sha256' not in page
        assert read_text_file(ops, str(path), offset=0, limit=4)['content'] == '前缀\r\n'
    saved = audits(ops)
    assert saved and 'local-secret-abcdef' not in json.dumps(saved)
    assert all('expected_bytes_sha256' not in (row.get('result') or {}) for row in saved)


@pytest.mark.parametrize(('codec', 'prefix'), [('gbk', b''), ('utf-16-le', codecs.BOM_UTF16_LE),
    ('utf-16-be', codecs.BOM_UTF16_BE)])
def test_append_preserves_charset_bom_crlf_and_byte_increment(ops, tmp_path, codec, prefix):
    path = tmp_path / 'append.txt'
    old = '第一行\r\n'
    path.write_bytes(prefix + old.encode(codec))
    encoding = 'gbk' if codec == 'gbk' else 'auto'
    with access(ops):
        result = write_text_file(ops, str(path), '第二行\n', encoding=encoding, append=True)
        expected = prefix + (old + '第二行\r\n').encode(codec)
        assert path.read_bytes() == expected
        assert result['bytes_written'] == len('第二行\r\n'.encode(codec))
        assert result['eol'] == 'crlf' and 'expected_bytes_sha256' not in result


def test_edit_unique_match_literal_text_and_errors_are_audited(ops, tmp_path):
    path = tmp_path / 'edit.txt'
    path.write_bytes(b'old\r\nkeep \\n old')
    with access(ops):
        before = path.read_bytes()
        with pytest.raises(ValueError, match='不唯一'):
            edit_text_file(ops, str(path), 'old', 'new')
        assert path.read_bytes() == before
        result = edit_text_file(ops, str(path), 'old', 'new', replace_all=True)
        assert result['replacements'] == 2 and path.read_bytes() == before.replace(b'old', b'new')
        edit_text_file(ops, str(path), r'\n', r'\t')
        assert b'\\t' in path.read_bytes()
    assert any(row['operation'] == 'edit_host_file' and row['error'] for row in audits(ops))


def test_ambiguous_charset_names_candidates_and_unencodable_write_keeps_bytes(ops, tmp_path):
    path = tmp_path / 'legacy.txt'
    original = '中文'.encode('gbk')
    path.write_bytes(original)
    with access(ops):
        with pytest.raises(ValueError, match='gb18030.*big5'):
            read_text_file(ops, str(path))
        assert read_text_file(ops, str(path), encoding='gbk')['content'] == '中文'
        with pytest.raises(ValueError, match='编码'):
            write_text_file(ops, str(path), '😀', encoding='gbk')
        assert path.read_bytes() == original
    assert sum(bool(row['error']) for row in audits(ops)) == 2


def test_wrapper_captures_intervening_update_without_overwrite(ops, tmp_path, monkeypatch):
    from narrative import host_text
    path = tmp_path / 'race.txt'
    path.write_bytes(b'original')
    backend = host_text.write_text

    def concurrent(*args, **kwargs):
        path.write_bytes(b'other writer')
        return backend(*args, **kwargs)

    monkeypatch.setattr(host_text, 'write_text', concurrent)
    with access(ops), pytest.raises(ValueError, match='改变'):
        write_text_file(ops, str(path), 'my update', append=True)
    assert path.read_bytes() == b'other writer'
    assert audits(ops)[0]['error']
