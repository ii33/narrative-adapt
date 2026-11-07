"""Command filtering must not change the byte encoding or literal text transport."""
import codecs
import sys
from pathlib import Path

import pytest
from test_assistant_host import access, wait
from test_assistant_host import ops as ops

from narrative.assistant_host import _CommandByteRedactor

SECRET = '私密-token-值'
TEXT = '前文\r\n' + SECRET + '\n字面 \\n \\u4e2d C:\\tmp\\file "引文" $x `y`\r尾文'
MARKER = '[REDACTED]'
CASES = [
    ('utf-8', b'', 'auto'),
    ('utf-8', codecs.BOM_UTF8, 'utf-8-sig'),
    ('gbk', b'', 'gbk'),
    ('gb18030', b'', 'gb18030'),
    ('big5', b'', 'big5'),
    ('utf-16-le', b'', 'utf-16le'),
    ('utf-16-be', b'', 'utf-16be'),
    ('utf-16-le', codecs.BOM_UTF16_LE, 'auto'),
    ('utf-16-be', codecs.BOM_UTF16_BE, 'auto'),
    ('utf-32-le', b'', 'utf-32-le'),
    ('utf-32-be', b'', 'utf-32-be'),
    ('utf-32-le', codecs.BOM_UTF32_LE, 'utf-32'),
    ('utf-32-be', codecs.BOM_UTF32_BE, 'utf-32'),
]


@pytest.mark.parametrize(('codec', 'bom', 'encoding'), CASES)
@pytest.mark.parametrize('has_secret', [False, True])
def test_command_output_preserves_encoding_bom_eols_and_literal_escapes(ops, codec, bom, encoding, has_secret):
    text = TEXT if has_secret else TEXT.replace(SECRET, '普通文字')
    raw = bom + text.encode(codec)
    expected = text.replace(SECRET, MARKER)
    with access(ops, secrets=(SECRET,)):
        started = ops.run_host_process(
            sys.executable, ['-c', 'import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))', raw.hex()],
            encoding=encoding,
        )
        result = wait(ops, started['command_id'])
        assert result['content'] == expected
        assert result['decoding_status'] == 'decoded' and result['complete']
        assert result['status'] == 'completed' and result['exit_code'] == 0
        retained = Path(result['raw_output_path']).read_bytes()
        assert retained == bom + expected.encode(codec)
        assert SECRET.encode(codec) not in retained
        assert Path(result['output_path']).read_bytes() == expected.encode('utf-8')
        reread = ops.read_host_command(started['command_id'], encoding=encoding)
        assert reread['content'] == expected and reread['complete'] and reread['redecoded']


@pytest.mark.parametrize(('codec', 'bom', 'encoding'), CASES)
def test_byte_filter_handles_every_split_and_never_inserts_extra_boms(codec, bom, encoding):
    text = '前' + SECRET + SECRET + '\r\n\\n尾'
    raw = bom + text.encode(codec)
    expected = bom + text.replace(SECRET, MARKER).encode(codec)
    for split in range(len(raw) + 1):
        redactor = _CommandByteRedactor((SECRET,), encoding)
        assert redactor.feed(raw[:split]) + redactor.feed(raw[split:]) + redactor.feed(b'', final=True) == expected
    redactor = _CommandByteRedactor((SECRET,), encoding)
    assert b''.join(redactor.feed(bytes([byte])) for byte in raw) + redactor.feed(b'', final=True) == expected


@pytest.mark.parametrize('codec', ['utf-8', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be', 'gb18030'])
def test_command_secret_and_multibyte_characters_cross_actual_read_blocks(ops, codec):
    # Every code-unit width crosses a collector block while the secret is incomplete.
    text = 'x' * (8192 // len('x'.encode(codec)) - 2) + SECRET + '😀\r\n\\n尾'
    with access(ops, secrets=(SECRET,)):
        result = wait(ops, ops.run_host_process(sys.executable, [
            '-c', 'import sys;sys.stdout.buffer.write(sys.argv[1].encode(sys.argv[2]))', text, codec,
        ], encoding=codec)['command_id'])
        assert result['content'] == text.replace(SECRET, MARKER)
        assert Path(result['raw_output_path']).read_bytes() == text.replace(SECRET, MARKER).encode(codec)
        assert result['complete'] and result['exit_code'] == 0


@pytest.mark.parametrize('codec', ['utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be', 'gbk'])
def test_wrong_declared_encoding_can_be_corrected_after_filtering_without_rerun(ops, tmp_path, codec):
    once = tmp_path / 'executions.txt'
    raw = TEXT.encode(codec)
    with access(ops, secrets=(SECRET,)):
        started = ops.run_host_process(sys.executable, [
            '-c', 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+"x" if p.exists() else "x"); sys.stdout.buffer.write(bytes.fromhex(sys.argv[2]))',
            str(once), raw.hex(),
        ], encoding='utf-8')
        result = wait(ops, started['command_id'])
        assert result['decoding_status'] == 'needs_encoding' and not result['complete']
        assert SECRET.encode(codec) not in Path(result['raw_output_path']).read_bytes()
        resolved = ops.read_host_command(started['command_id'], encoding=codec)
        assert resolved['content'] == TEXT.replace(SECRET, MARKER)
        assert resolved['complete'] and resolved['redecoded']
        assert once.read_text() == 'x'


@pytest.mark.parametrize(('codec', 'bom', 'encoding'), CASES)
def test_overlapping_prefix_secrets_use_one_longest_replacement(codec, bom, encoding):
    text = 'prefix-private-long suffix'
    raw = bom + text.encode(codec)
    expected = bom + ('prefix-' + MARKER + ' suffix').encode(codec)
    for secrets in [('private', 'private-long'), ('private-long', 'private')]:
        redactor = _CommandByteRedactor(secrets, encoding)
        assert redactor.feed(raw) + redactor.feed(b'', final=True) == expected


def test_bomless_ambiguous_unicode_bytes_use_longest_pattern():
    # This byte stream also means 'A\0' in UTF-16LE. Without a BOM, retained
    # bytes cannot promise the same marker under both possible interpretations.
    redactor = _CommandByteRedactor(('A',), 'auto')
    raw = redactor.feed(b'A\0\0\0') + redactor.feed(b'', final=True)
    assert raw == MARKER.encode('utf-32-le')
    assert raw.decode('utf-32-le') == MARKER


@pytest.mark.parametrize('raw', [b'', b'x', b'xy', b'xyz', codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF32_BE])
def test_empty_short_and_bom_only_output_is_retained_exactly(raw):
    for secrets in [(), (SECRET,)]:
        redactor = _CommandByteRedactor(secrets, 'auto')
        assert redactor.feed(raw) + redactor.feed(b'', final=True) == raw


def test_byte_filter_does_not_match_newly_inserted_marker_text():
    redactor = _CommandByteRedactor(('private', 'REDACTED'), 'utf-8')
    assert redactor.feed(b'private') + redactor.feed(b'', final=True) == b'[REDACTED]'


@pytest.mark.parametrize(('codec', 'bom', 'text'), [
    ('utf-16-le', codecs.BOM_UTF16_LE, '䄀\0|A'),
    ('utf-16-be', codecs.BOM_UTF16_BE, '\0䄀|A'),
    ('utf-32-le', codecs.BOM_UTF32_LE, '䄀\0|A'),
    ('utf-32-be', codecs.BOM_UTF32_BE, '\0䄀|A'),
])
def test_bom_known_filter_ignores_unaligned_hits_and_finds_later_real_secret(codec, bom, text):
    raw = bom + text.encode(codec)
    expected = bom + text.replace('A', MARKER).encode(codec)
    for split in range(len(raw) + 1):
        redactor = _CommandByteRedactor(('A',), 'auto')
        assert redactor.feed(raw[:split]) + redactor.feed(raw[split:]) + redactor.feed(b'', final=True) == expected
