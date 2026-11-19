"""导入耗时基准：把 ``import narrative.native_bridge`` 拆到模块一级。

为什么量法和 ``startup_bench.py`` 不同：那边量的是「整个启动」，这里只回答一个问题——
「native_bridge 的 1.5 秒花在谁身上」。所以本脚本跑 ``python -X importtime`` 的子进程，
把 importtime 的原始表解析成一棵树，再按「谁直接被 native_bridge 拉进来」排序。

为什么默认先导 QtGui/QtQml：真实启动在这之前已经导过 QtGui 了，PySide6.QtCore 那三百
毫秒不属于 native_bridge 的账。``--no-qt-prelude`` 用来看一个「裸进程」的数字，两者不可
混着比。

为什么要 ``--repeat``：子进程冷启动受磁盘缓存和杀毒扫描影响，单次数十毫秒的抖动是常事；
重复 N 次取中位数，再断言「谁进了 imports」这种结构性事实（对所有次数都成立才算）。

用法::

    uv run python scripts/import_bench.py                       # 3 次中位数 + 直接子模块表
    uv run python scripts/import_bench.py --children            # 顺带列出直接子模块
    uv run python scripts/import_bench.py --json .dsh/import-before.json
    uv run python scripts/import_bench.py --baseline .dsh/import-before.json
    uv run python scripts/import_bench.py --lazy-costs          # 首次开工程 / 首次发消息的增量

``--lazy-costs`` 量的是「已经 import 完 native_bridge 之后，再导这批模块还要多久」——也就是
惰性化之后，成本被挪到首次用到它的那个槽位上要花的时间。

退出码：0 正常；2 环境缺依赖（没装 PySide6 之类）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TARGET = "narrative.native_bridge"
MARKER = "__IMPORT_BENCH_JSON__ "

# 目标要压掉的模块：出现在这份清单里的，导入完目标模块后就不该再被导进来。
FORBIDDEN = (
    "narrative.harness",
    "narrative.graph_operations",
    "narrative.graph_profile_ops",
    "narrative.project_catalog",
    "pydantic",
)

# 「某个槽位第一次用到才导入」的分组，--lazy-costs 用它们量增量成本。
LAZY_GROUPS = (
    ("打开工程", ("narrative.project_catalog", "narrative.graph_operations",
                  "narrative.graph_profile_ops")),
    ("发消息", ("narrative.harness",)),
)

_LINE = "import time:"


def _parse_trace(text: str) -> list[dict]:
    """把 ``-X importtime`` 的输出折成带父子关系的记录。

    **行的顺序是模块导入完成的那一刻**（子模块先于父模块、子树的最后一行才是父模块），
    所以父子关系要倒着读：把记录反过来过一遍，维护一条「当前祖先」的栈——遇到 depth 不
    比栈顶深就把栈顶弹掉，剩下的栈顶就是它的父模块。名字前缀每深一层多两个空格。
    解析不了的行（例如 stdout 里混进来的别的东西）直接跳过。
    """
    flat: list[dict] = []
    for raw in text.splitlines():
        if not raw.startswith(_LINE):
            continue
        parts = raw[len(_LINE):].split("|", 2)
        if len(parts) != 3:
            continue
        self_part, cumulative_part, name_part = parts
        try:
            self_us = int(self_part.strip())
            cumulative_us = int(cumulative_part.strip())
        except ValueError:
            continue
        name = name_part.strip()
        if not name:
            continue
        flat.append({
            "module": name,
            "depth": (len(name_part) - len(name_part.lstrip(" "))) // 2,
            "self_ms": round(self_us / 1000.0, 1),
            "cumulative_ms": round(cumulative_us / 1000.0, 1),
        })

    entries: list[dict] = [None] * len(flat)  # type: ignore[list-item]
    stack: list[dict] = []
    for index in range(len(flat) - 1, -1, -1):
        entry = flat[index]
        while stack and stack[-1]["depth"] >= entry["depth"]:
            stack.pop()
        entry["parent"] = stack[-1]["module"] if stack else None
        entry["parent_index"] = stack[-1]["index"] if stack else None
        entry["index"] = index
        entries[index] = entry
        stack.append(entry)
    return entries


def _descendants(entries: list[dict], root_index: int) -> list[dict]:
    """root 的整棵子树（不含 root 自己）；父索引小于子索引，所以一趟就能带下去。"""
    inside = {root_index}
    found: list[dict] = []
    for entry in entries:
        parent = entry.get("parent_index")
        if parent is not None and parent in inside:
            inside.add(entry["index"])
            found.append(entry)
    return found


def _worker(args: argparse.Namespace) -> int:
    """子进程：跑 -X importtime，把原始 stderr 与结果打成一行 JSON。"""
    prelude = ""
    if args.qt_prelude:
        # 真实启动在 import native_bridge 之前已经导过 QtGui/QtQml，这里对齐。
        prelude = "import PySide6.QtGui, PySide6.QtQml\n"
    program = f"{prelude}import {args.target}\n"
    names = [name for name in (args.incremental or "").split(",") if name]
    if names:
        # 只在这里量「已经导入目标之后，再导入这批模块」的增量。
        program = (
            f"{prelude}import {args.target}\n"
            "import time\n"
            "started = time.perf_counter()\n"
            "for name in " + repr(names) + ":\n"
            "    __import__(name)\n"
            "print('INCREMENTAL', ','.join(" + repr(names) + "),"
            " round(1000.0 * (time.perf_counter() - started), 1))\n"
        )
    command = [sys.executable, "-X", "importtime", "-c", program]
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=args.timeout,
                               check=False)
    if completed.returncode != 0:
        print(completed.stderr or completed.stdout, file=sys.stderr)
        return 2
    entries = _parse_trace(completed.stderr)
    if not entries:
        print("没有解析到 importtime 输出：", file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        return 2
    target = next((e for e in entries if e["module"] == args.target), None)
    incremental = []
    for line in completed.stdout.splitlines():
        if line.startswith("INCREMENTAL "):
            _, group, milliseconds = line.split()
            incremental.append({"modules": group.split(","), "cost_ms": float(milliseconds)})
    payload = {
        "target": target,
        "entries": entries,
        "incremental": incremental,
        "python": sys.version.split()[0],
        "qt_prelude": bool(args.qt_prelude),
    }
    print(MARKER + json.dumps(payload, ensure_ascii=False))
    return 0


def _run(args: argparse.Namespace, index: int, group: tuple[str, ...] | None = None) -> dict | None:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker",
               "--target", args.target, "--timeout", str(args.timeout)]
    if args.qt_prelude:
        command.append("--qt-prelude")
    if group:
        command.extend(["--incremental", ",".join(group)])
    label = f"[run {index + 1}/{args.repeat}]"
    print(f"{label} {' '.join(group) if group else ''}".rstrip(), file=sys.stderr)
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=args.timeout,
                               check=False)
    if completed.returncode != 0:
        print(completed.stderr or completed.stdout, file=sys.stderr)
        return None
    for line in completed.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    print(completed.stderr, file=sys.stderr)
    return None


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _direct_children(runs: list[dict], target: str) -> dict[str, dict[str, float]]:
    """目标模块的直接子模块：每次运行都取，最后收集中位数（self 与 cumulative）。"""
    collected: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        for entry in run["entries"]:
            if entry["parent"] != target:
                continue
            bucket = collected.setdefault(entry["module"], {"self": [], "cumulative": []})
            bucket["self"].append(float(entry["self_ms"]))
            bucket["cumulative"].append(float(entry["cumulative_ms"]))
    return {
        module: {"self": _median(values["self"]), "cumulative": _median(values["cumulative"])}
        for module, values in collected.items()
    }


def _subtree_cumulative(run: dict, root: str) -> dict[str, float]:
    """root 子树里每个模块的 cumulative（importtime 的 cumulative 本身就是累计值）。"""
    entries = run["entries"]
    root_entry = next((e for e in entries if e["module"] == root), None)
    if root_entry is None:
        return {}
    return {entry["module"]: float(entry["cumulative_ms"])
            for entry in _descendants(entries, root_entry["index"])}


def _summary(runs: list[dict], args: argparse.Namespace) -> dict:
    targets = [float(run["target"]["cumulative_ms"]) for run in runs if run["target"]]
    selves = [float(run["target"]["self_ms"]) for run in runs if run["target"]]
    subtree: dict[str, list[float]] = {}
    for run in runs:
        for module, value in _subtree_cumulative(run, args.target).items():
            subtree.setdefault(module, []).append(value)
    # 结构性事实：被禁止的模块是否还在目标模块的导入树里（任一次出现就算还在）。
    present = sorted({module for module in subtree if module in FORBIDDEN or
                      any(module == f or module.startswith(f + ".") for f in FORBIDDEN)})
    present_roots = sorted({f for f in FORBIDDEN
                            if any(m == f for m in subtree)})
    payload = {
        "repeat": args.repeat,
        "target": args.target,
        "qt_prelude": bool(args.qt_prelude),
        "python": runs[0].get("python"),
        "cumulative_ms_median": round(_median(targets), 1),
        "self_ms_median": round(_median(selves), 1),
        "direct_children": _direct_children(runs, args.target),
        "forbidden_present": present,
        "forbidden_roots_present": present_roots,
        "incremental": [
            {"modules": item["modules"], "cost_ms": item["cost_ms"]}
            for run in runs for item in run.get("incremental", [])
        ],
    }
    if payload["incremental"]:
        grouped: dict[str, list[float]] = {}
        for item in payload["incremental"]:
            grouped.setdefault(",".join(item["modules"]), []).append(item["cost_ms"])
        payload["incremental_median"] = {
            key: round(_median(values), 1) for key, values in grouped.items()
        }
    return payload


def _print_report(summary: dict, baseline: dict | None) -> None:
    print()
    print(f"目标：import {summary['target']}"
          f"（{summary['repeat']} 次独立进程冷启动，"
          f"{'已先导 QtGui/QtQml' if summary['qt_prelude'] else '裸进程'}）")
    print(f"  cumulative 中位数：{summary['cumulative_ms_median']:8.0f}ms"
          f"   self：{summary['self_ms_median']:7.0f}ms")
    if baseline is not None:
        before = baseline.get("cumulative_ms_median", 0.0)
        after = summary["cumulative_ms_median"]
        print(f"  基线（{baseline.get('target')}）：{before:8.0f}ms"
              f"   → 变化 {after - before:+.0f}ms"
              f"（{(after - before) / before * 100 if before else 0:+.0f}%）")

    children = summary.get("direct_children") or {}
    if children:
        print()
        title = "直接子模块（cumulative，中位数）"
        print(f"{title:34s} {'cumulative':>11s} {'self':>9s}")
        print("-" * 58)
        for module, values in sorted(children.items(),
                                     key=lambda item: item[1]["cumulative"], reverse=True):
            print(f"{module:34s} {values['cumulative']:10.0f}ms {values['self']:8.0f}ms")

    present = summary.get("forbidden_roots_present") or []
    print()
    if present:
        print("启动时仍被导入的重模块：" + "、".join(present))
    else:
        print("启动时仍被导入的重模块：（无——" + "、".join(FORBIDDEN) + " 都不在导入树里）")

    incremental = summary.get("incremental_median") or {}
    if incremental:
        print()
        print("惰性化之后的增量成本（已导入目标模块后再导这批）：")
        for modules, cost in incremental.items():
            print(f"  {modules:70s} {cost:8.0f}ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="native_bridge 导入耗时基准")
    parser.add_argument("--target", default=TARGET, help=f"要量的模块（默认 {TARGET}）")
    parser.add_argument("--repeat", type=int, default=3, help="独立进程次数，取中位数（默认 3）")
    parser.add_argument("--json", type=Path, default=None, help="把中位数结果写成 JSON")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="与之前写下的 JSON 对比，打印改前/改后")
    parser.add_argument("--children", action="store_true",
                        help="打印直接子模块表（默认也打印；留着是为了显式写出意图）")
    parser.add_argument("--no-qt-prelude", dest="qt_prelude", action="store_false",
                        help="不先导 QtGui/QtQml（裸进程数字，不能和默认数字混比）")
    # 内部转发的正向开关；default=SUPPRESS 是为了不改掉上面那个 store_false 的默认值。
    parser.add_argument("--qt-prelude", dest="qt_prelude", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--lazy-costs", action="store_true",
                        help="额外量「首次打开工程 / 首次发消息」的增量导入成本")
    parser.add_argument("--timeout", type=float, default=180.0, help="单次运行超时秒数")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--incremental", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        return _worker(args)

    if args.repeat < 1:
        parser.error("--repeat 至少为 1")

    runs: list[dict] = []
    if args.lazy_costs:
        # 每组各起独立进程：组和组之间会共享 pydantic 之类的依赖，串在一个进程里量，
        # 后面那组会因为前面已经导过而显得便宜——那不是「首次用到」的真实成本。
        for _, group in LAZY_GROUPS:
            runs.extend(result for result in
                        (_run(args, index, group) for index in range(args.repeat))
                        if result is not None)
    else:
        runs = [result for result in (_run(args, index) for index in range(args.repeat))
                if result is not None]
    if not runs:
        return 2

    summary = _summary(runs, args)
    baseline = None
    if args.baseline is not None and args.baseline.is_file():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    _print_report(summary, baseline)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
