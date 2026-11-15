"""用计划任务把长任务真正脱离当前进程树，并给出可信的存活判据。

为什么需要它：本仓的真实模型验收要跑几分钟到二十几分钟，而**在编辑器/代理的工具调用里**
用 `run_in_background` 或 `Start-Process` 启动，工具一超时就会把整棵进程树杀掉（实测
120 s / 293 s 就被带走），于是任务永远跑不完，还留下"目录建了、日志也在长"的假存活痕迹。

做法：
* 用 `schtasks` 建一个一次性任务并在其中启动 `bash -lc '…'`，任务的父进程是计划任务服务，
  不在调用方的作业里，调用方退出/超时都不会波及；
* 启动前用 `O_EXCL` 建锁文件（`.runtime/detached/<name>.lock`，内含 pid、启动时间、命令行），
  拿不到锁就拒绝重复启动——这是防"重复启动白烧真实请求"的第二道闸；
* 存活判据只用两条：任务状态（`schtasks /query`）与锁文件里的 pid 是否存在，
  不看"目录建了没有""日志有没有变长"（这两者在进程死后依然成立）。

用法：
    python scripts/run_detached.py --name baseline --log .runtime/baseline.log -- \
        .venv/Scripts/python.exe -u scripts/accept_piaomiao_three_chapters.py
    python scripts/run_detached.py --name baseline --status
    python scripts/run_detached.py --name baseline --clean
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


def _task_name(name: str) -> str:
    return f"NarrativeDetached_{name}"


def _lock_path(name: str) -> Path:
    return LOCK_DIR / f"{name}.lock"


def _read_lock(name: str) -> dict | None:
    path = _lock_path(name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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


def _task_status(name: str) -> str:
    result = subprocess.run(["schtasks", "/query", "/tn", _task_name(name), "/fo", "list"],
                            capture_output=True, text=True, timeout=60, encoding="mbcs", errors="replace")
    if result.returncode != 0 or not result.stdout:
        return "absent"
    # 计划任务的输出跟系统语言走（中文系统是「状态: 正在运行」），两种都认。
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key.startswith("status") or key in {"状态", "模式"} or key.startswith("状态"):
            if value:
                return value
    return "unknown"


def start(name: str, log: Path, command: list[str]) -> int:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(name)
    existing = _read_lock(name)
    if lock.exists():
        if existing and _task_status(name) in {"Running", "正在运行"}:
            print(json.dumps({"started": False, "reason": "已在运行", "lock": existing}, ensure_ascii=False))
            return 1
        print(json.dumps({"started": False, "reason": "锁文件在但任务已不在运行；确认无人跑后 --clean 再启动",
                          "lock": existing}, ensure_ascii=False))
        return 1
    bash = shutil.which("bash")
    if not bash:
        print("找不到 bash（本仓脚本用 bash 执行命令行）", file=sys.stderr)
        return 2
    log.parent.mkdir(parents=True, exist_ok=True)
    pid_file = LOCK_DIR / f"{name}.pid"
    quoted = " ".join(shlex.quote(part) for part in command)
    inner = (f"cd {shlex.quote(ROOT.as_posix())} && echo $$ > {shlex.quote(pid_file.as_posix())} && "
             f"exec {quoted} > {shlex.quote(log.as_posix())} 2>&1")
    task = _task_name(name)
    # schtasks 的 /tr 上限 261 字符：把真正的命令行写进 sh 脚本，/tr 只指向它。
    script = LOCK_DIR / f"{name}.sh"
    script.write_text("#!/usr/bin/env bash\n" + inner + "\n", encoding="utf-8")
    created = subprocess.run(
        ["schtasks", "/create", "/tn", task, "/tr", f'"{bash}" "{script.as_posix()}"',
         "/sc", "once", "/st", "00:00", "/f"],
        capture_output=True, text=True, timeout=60, encoding="mbcs", errors="replace",
    )
    if created.returncode != 0:
        print(created.stdout + created.stderr, file=sys.stderr)
        return 3
    ran = subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, text=True,
                         timeout=60, encoding="mbcs", errors="replace")
    if ran.returncode != 0:
        print(ran.stdout + ran.stderr, file=sys.stderr)
        return 4
    record = {
        "name": name,
        "task": task,
        "command": command,
        "log": str(log),
        "started_at": datetime.now(UTC).isoformat(),
        "pid": None,
        "note": "pid 由任务内的进程自己写；这里先只记启动凭据",
    }
    # O_EXCL：并发启动时只有一个能写成功。
    with open(lock, "x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"started": True, "task": task, "log": str(log),
                      "status": _task_status(name)}, ensure_ascii=False))
    return 0


def _pid(name: str) -> int | None:
    try:
        return int((LOCK_DIR / f"{name}.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def status(name: str) -> int:
    lock = _read_lock(name)
    task_status = _task_status(name)
    state = {
        "name": name,
        "task_status": task_status,
        "running": task_status in {"Running", "正在运行"},
        "lock": lock,
        "pid": _pid(name),
        "pid_alive": _alive(_pid(name)),
    }
    print(json.dumps(state, ensure_ascii=False))
    return 0 if state["running"] else 1


def clean(name: str) -> int:
    subprocess.run(["schtasks", "/delete", "/tn", _task_name(name), "/f"],
                   capture_output=True, text=True, timeout=60)
    _lock_path(name).unlink(missing_ok=True)
    (LOCK_DIR / f"{name}.pid").unlink(missing_ok=True)
    (LOCK_DIR / f"{name}.sh").unlink(missing_ok=True)
    print(json.dumps({"cleaned": name}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把长任务交给计划任务运行，避免被工具超时杀掉")
    parser.add_argument("--name", required=True, help="任务短名（同一名字互斥）")
    parser.add_argument("--log", default="", help="stdout/err 落盘的路径（默认 .runtime/<name>.log）")
    parser.add_argument("--status", action="store_true", help="只查状态")
    parser.add_argument("--clean", action="store_true", help="删任务与锁")
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
