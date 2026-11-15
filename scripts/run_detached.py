"""把长任务真正脱离工具作业与控制台运行，并给出可信的存活判据。

本会话实测过的两条死路（都发生在"跑一次真实模型验收"上）：
* `run_in_background` / `Start-Process` 这类工具作业：调用方超时会**杀掉整棵进程树**，
  几分钟以上的任务永远跑不完，还会留下"目录建了、日志也在长"的假存活痕迹；
* `schtasks` 计划任务：任务归属启动它的交互会话，控制台一关就收到 Ctrl+C
  （Last Result 实测 `0xC000013A`），同样跑不完。

所以这里用 `CreateProcess` 的 `DETACHED_PROCESS | BREAKAWAY_FROM_JOB | NEW_PROCESS_GROUP |
NO_WINDOW` 起进程：既脱离调用方的作业对象，也没有自己的控制台，调用方退出、超时都不波及。

用法：
    python scripts/run_detached.py --name baseline1 --log .runtime/baseline-ch1.log -- \
        .venv/Scripts/python.exe -u scripts/accept_piaomiao_three_chapters.py
    python scripts/run_detached.py --name baseline1 --status
    python scripts/run_detached.py --name baseline1 --clean

存活判据只用「锁文件里记的 pid 还在不在」（`tasklist` 只读查询）。**不要**拿"目录建了没有"
"日志有没有变长"当存活证据：进程死后这两者依然成立。
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / ".runtime" / "detached"

# Win32 进程创建标志：脱离作业对象 / 无控制台 / 独立进程组 / 不弹窗。
DETACHED_PROCESS = 0x00000008
BREAKAWAY_FROM_JOB = 0x01000000
NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATION_FLAGS = DETACHED_PROCESS | BREAKAWAY_FROM_JOB | NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _lock_path(name: str) -> Path:
    return LOCK_DIR / f"{name}.lock"


def _pid_path(name: str) -> Path:
    return LOCK_DIR / f"{name}.pid"


def _read_lock(name: str) -> dict | None:
    try:
        return json.loads(_lock_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    """只读地查进程在不在。

    不能用 `os.kill(pid, 0)`：Windows 上 os.kill 对任意信号都会走 TerminateProcess，
    探活会顺手把目标进程杀掉（真踩过）。这里用 tasklist 的只读查询。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                            text=True, encoding="mbcs", errors="replace", timeout=30)
    return str(pid) in (result.stdout or "")


def _recorded_pid(name: str) -> int | None:
    try:
        return int(_pid_path(name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        lock = _read_lock(name) or {}
        pid = lock.get("pid")
        return pid if isinstance(pid, int) else None


def start(name: str, log: Path, command: list[str]) -> int:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    if _lock_path(name).exists():
        pid = _recorded_pid(name)
        if _alive(pid):
            print(json.dumps({"started": False, "reason": "已在运行", "pid": pid}, ensure_ascii=False))
            return 1
        print(json.dumps({"started": False, "reason": "锁文件在但进程已不在；先 --clean 再启动",
                          "pid": pid}, ensure_ascii=False))
        return 1
    bash = shutil.which("bash")
    if not bash:
        print("找不到 bash（本仓脚本用 bash 执行命令行）", file=sys.stderr)
        return 2
    log.parent.mkdir(parents=True, exist_ok=True)
    pid_path = _pid_path(name)
    quoted = " ".join(shlex.quote(part) for part in command)
    inner = (f"cd {shlex.quote(ROOT.as_posix())} && echo $$ > {shlex.quote(pid_path.as_posix())} && "
             f"exec {quoted} > {shlex.quote(log.as_posix())} 2>&1")
    script = LOCK_DIR / f"{name}.sh"
    script.write_text("#!/usr/bin/env bash\n" + inner + "\n", encoding="utf-8")
    handle = open(log, "ab")
    try:
        child = subprocess.Popen([bash, str(script)], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                 stdout=handle, stderr=subprocess.STDOUT, close_fds=True,
                                 creationflags=CREATION_FLAGS)
    except OSError as exc:
        print(json.dumps({"started": False, "reason": f"启动失败：{exc}"}, ensure_ascii=False))
        return 5
    finally:
        handle.close()
    record = {
        "name": name,
        "command": command,
        "log": str(log),
        "started_at": datetime.now(UTC).isoformat(),
        "pid": child.pid,
        "mode": "detached_process + breakaway_from_job",
    }
    with open(_lock_path(name), "x", encoding="utf-8") as write:
        json.dump(record, write, ensure_ascii=False, indent=2)
    print(json.dumps({"started": True, "pid": child.pid, "log": str(log)}, ensure_ascii=False))
    return 0


def status(name: str) -> int:
    pid = _recorded_pid(name)
    state = {"name": name, "pid": pid, "running": _alive(pid),
             "lock": _read_lock(name)}
    print(json.dumps(state, ensure_ascii=False))
    return 0 if state["running"] else 1


def clean(name: str) -> int:
    _lock_path(name).unlink(missing_ok=True)
    _pid_path(name).unlink(missing_ok=True)
    (LOCK_DIR / f"{name}.sh").unlink(missing_ok=True)
    print(json.dumps({"cleaned": name}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把长任务脱离工具作业运行")
    parser.add_argument("--name", required=True, help="任务短名（同一名字互斥）")
    parser.add_argument("--log", default="", help="stdout/err 落盘路径（默认 .runtime/<name>.log）")
    parser.add_argument("--status", action="store_true", help="只查状态")
    parser.add_argument("--clean", action="store_true", help="删锁与 pid 文件")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后的命令行")
    args = parser.parse_args()
    if args.status:
        return status(args.name)
    if args.clean:
        return clean(args.name)
    command = [item for item in args.command if item != "--"]
    if not command:
        parser.error("需要 -- 之后的命令行")
    log = Path(args.log) if args.log else ROOT / ".runtime" / f"{args.name}.log"
    return start(args.name, log, command)


if __name__ == "__main__":
    raise SystemExit(main())
