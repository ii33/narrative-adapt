"""Real local argv/stdin/scripts use the existing single command lifecycle."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from test_assistant_host import access, wait
from test_assistant_host import ops as ops

from narrative.project_package import layout


def test_process_argv_is_literal_and_default_cwd_is_project_entry(ops, tmp_path):
    marker = tmp_path / 'must-not-exist'
    values = ['中文 空格', '', 'a"b', '甲\n乙', '& echo injected > ' + str(marker), '$(echo injected)', '`echo nope`']
    with access(ops):
        started = ops.run_host_process(sys.executable, ['-X', 'utf8', '-c', 'import json,sys,os;print(json.dumps([sys.argv[1:],os.getcwd()],ensure_ascii=False))', *values])
        result = wait(ops, started['command_id'])
        actual, cwd = json.loads(result['content'])
        assert actual == values and not marker.exists()
        assert Path(cwd) == layout(ops.project_info().path).entry.parent
        assert result['exit_code'] == 0 and result['decoding_status'] == 'decoded'


def test_chinese_multiline_stdin_and_script_text_are_not_shell_escaped(ops):
    payload = '甲\r\n乙\n$variable `quote` "双引号" \\路径 😀'
    script = 'import sys\ntext=sys.stdin.read()\nprint(repr(text))\n'
    with access(ops):
        started = ops.run_host_script('python', script, stdin_text=payload)
        result = wait(ops, started['command_id'])
        # Python text stdin normalizes CRLF itself; the transport bytes stay exact.
        assert result['content'].strip() == repr(payload.replace('\r\n', '\n'))
        assert result['stdin_bytes_written'] == len(payload.encode('utf-8'))
        echoed = wait(ops, ops.run_host_process(sys.executable, ['-c', "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read().decode('gb18030').encode('utf-8'))"], stdin_text=payload, input_encoding='gb18030')['command_id'])
        assert echoed['content'] == payload
        assert echoed['input_encoding'] == 'gb18030' and echoed['detected_encoding'] == 'utf-8'
        root = Path(result['output_path']).parent
        assert not list(root.glob('.command-script-*'))
        audits = [json.loads(p.read_text(encoding='utf-8')) for p in root.glob('operation-*.json')]
        assert any(a['args'].get('script') == script for a in audits)


def test_cancel_blocked_large_stdin_settles_without_deadlock(ops):
    event = threading.Event()
    with access(ops, event=event):
        start = time.monotonic()
        command = ops.run_host_process(sys.executable, ['-c', 'import time;time.sleep(60)'], stdin_text='甲'*2_000_000)
        assert time.monotonic() - start < 5
        event.set()
    # Cancelled tasks cannot use host tools, inspect terminal audit only after stop.
    with access(ops):
        result = wait(ops, command['command_id'])
        assert result['status'] == 'cancelled'


def test_output_bytes_are_retained_and_encoding_can_be_fixed_without_execution(ops, tmp_path):
    marker = tmp_path / 'once.txt'
    with access(ops):
        command = ops.run_host_process(sys.executable, ['-c', 'import sys,pathlib;pathlib.Path(sys.argv[1]).write_text("once");sys.stdout.buffer.write(bytes.fromhex("a4a4"));sys.exit(7)', str(marker)])
        result = wait(ops, command['command_id'])
        assert result['exit_code'] == 7 and result['status'] == 'failed'
        assert result['decoding_status'] == 'needs_encoding'
        assert result['diagnostics'][0]['code'] == 'OUTPUT_ENCODING_REQUIRED'
        assert not result['complete'] and Path(result['raw_output_path']).read_bytes() == bytes.fromhex('a4a4')
        resolved = ops.read_host_command(command['command_id'], encoding='big5')
        assert resolved['content'] == '中' and resolved['complete']
        assert resolved['redecoded'] and marker.read_text() == 'once'


def test_invalid_arguments_and_missing_pwsh_reject_before_spawn(ops, monkeypatch):
    import narrative.assistant_host as host
    with access(ops):
        for kwargs in [{'program': 'a\x00b', 'args': []}, {'program': sys.executable, 'args': ['bad\x00arg']},
                       {'program': sys.executable, 'args': [], 'input_encoding': 'not-a-codec'},
                       {'program': sys.executable, 'args': [], 'encoding': 'not-a-codec'}]:
            with pytest.raises((ValueError, LookupError)):
                ops.run_host_process(**kwargs)
        monkeypatch.setattr(host.shutil, 'which', lambda name: None)
        with pytest.raises(ValueError, match='不回退'):
            ops.run_host_script('pwsh', 'Write-Output "中文"')


def test_raw_bytes_and_text_are_both_secret_redacted_across_chunks(ops):
    secret = 'host-command-private-key'
    code = 'import sys;sys.stdout.buffer.write(b"x"*8190+sys.argv[1].encode()+b"end")'
    with access(ops, secrets=(secret,)):
        result = wait(ops, ops.run_host_process(sys.executable, ['-c', code, secret])['command_id'])
        assert secret not in result['content']
        assert secret.encode() not in Path(result['raw_output_path']).read_bytes()
        assert b'[REDACTED]' in Path(result['raw_output_path']).read_bytes()


def test_legacy_encoded_secret_never_enters_raw_output_audit(ops):
    secret = '测试私密值'
    with access(ops, secrets=(secret,)):
        result = wait(ops, ops.run_host_process(sys.executable, ['-c', "import sys;sys.stdout.buffer.write(sys.argv[1].encode('gbk'))", secret], encoding='gbk')['command_id'])
        assert secret not in result['content']
        raw = Path(result['raw_output_path']).read_bytes()
        assert secret.encode('gbk') not in raw and b'[REDACTED]' in raw
