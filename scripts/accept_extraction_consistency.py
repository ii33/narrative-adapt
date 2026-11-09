"""提取提示词的一致性验收：同一篇小原文、同一模型，多次运行比较事件边界。

每个 run 都在新建的隔离工程里跑一次真实的提取任务，保存原始输出、工具调用、
自动入库结果与 usage；汇总里比较多次运行的事件数、事件起点与重复命名，用来判断
提示词改动是否让边界更稳定（而不是只看某一次跑得好不好）。

用法（在 narrative-adapt 目录下）：
  $env:OPENCODE_GO_API_KEY = "..."   # 或其它已配置的密钥变量
  .venv/Scripts/python.exe scripts/accept_extraction_consistency.py --output out/extract-compare/old --runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

STORY = ROOT / "examples" / "extraction_consistency_story.txt"

PROVIDERS = {
    "opencode-go": {
        "provider": "opencode-go",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "deepseek-v4.1-flash",
        "key_env": "OPENCODE_GO_API_KEY",
    },
    "ark": {
        "provider": "ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        "model": "Doubao-Seed-2.1-pro",
        "key_env": "ARK_API_KEY",
    },
}


def harness_settings(provider: str) -> dict:
    preset = PROVIDERS[provider]
    return {
        **preset,
        "reasoning_effort": None,
        "max_tokens": 32768,
        "context_window": 200_000,
        "output_reserve": 16384,
        "compaction_reserve": 8192,
        "compaction_max_tokens": 8192,
        "timeout_seconds": 120,
        "temperature": None,
    }


@contextmanager
def isolated_home(directory: Path):
    """把 Harness 的缓存/配置写进本次输出目录，不碰用户目录。"""
    previous = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
    home = directory / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def event_rows(ops) -> list[dict]:
    snapshot = ops.graph_snapshot()
    rows = []
    for node in snapshot["nodes"]:
        if node.get("node_type") != "event":
            continue
        spans = node.get("evidence_spans") or []
        rows.append(
            {
                "id": node["id"],
                "name": node.get("name", ""),
                "starts": [span.get("start") for span in spans if isinstance(span, dict)],
                "participants": len(node.get("participants") or []),
                "scene": node.get("scene") or (node.get("events") or [{}])[0].get("scene")
                if node.get("events")
                else node.get("scene"),
                "location": node.get("location"),
            }
        )
    return sorted(rows, key=lambda row: (row["starts"] or [10**9])[0])


def graph_dump(ops) -> dict:
    snapshot = ops.graph_snapshot()
    nodes = snapshot["nodes"]
    counts: dict[str, int] = {}
    for node in nodes:
        kind = node.get("node_type") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    events = event_rows(ops)
    scenes = [
        {"id": node["id"], "name": node.get("name", ""),
         "starts": [span.get("start") for span in node.get("evidence_spans") or [] if isinstance(span, dict)]}
        for node in nodes if node.get("node_type") == "scene"
    ]
    scenes.sort(key=lambda row: (row["starts"] or [10**9])[0])
    names: dict[str, int] = {}
    for row in events:
        names[row["name"]] = names.get(row["name"], 0) + 1
    return {
        "counts": counts,
        "nodes": len(nodes),
        "property_records": len(snapshot.get("objects") or []),
        "events": events,
        "scenes": scenes,
        "duplicate_event_names": {name: n for name, n in names.items() if n > 1},
        "check": ops.check_project().get("counts", {}),
    }


def chat_calls(project_path: Path) -> list[dict]:
    from narrative.chat_store import ChatStore
    from narrative.operations import Operations

    store = ChatStore(Operations.open(project_path).project_info().path)
    calls = []
    for chat in store.list():
        for message in store.load(chat["id"])["messages"]:
            for part in message.get("content") or []:
                if part.get("type") == "tool-call":
                    calls.append({"tool": part.get("name"), "arguments": part.get("arguments")})
    return calls


def wait_until(bridge, app, done, seconds: float) -> bool:
    ended = time.monotonic() + seconds
    while time.monotonic() < ended:
        app.processEvents()
        if done():
            for _ in range(20):
                app.processEvents()
                time.sleep(0.02)
            return True
        time.sleep(0.02)
    return False


def run_once(run_dir: Path, story: str, provider: str, seconds: float) -> dict:
    from PySide6.QtWidgets import QApplication

    from narrative.native_bridge import NativeBridge
    from narrative.operations import Operations

    run_dir.mkdir(parents=True, exist_ok=True)
    # 服务配置写进本次运行目录：既不碰用户目录，也不受外部 HOME 变化影响。
    os.environ["NARRATIVE_PROVIDER_DB"] = str(run_dir / "providers.sqlite3")
    app = QApplication.instance() or QApplication([])
    results: list[dict] = []
    errors: list[str] = []
    kinds: list[str] = []
    finished = False
    elapsed = 0.0
    usage: dict = {}

    def on_result(kind, payload):
        kinds.append(kind)
        if kind == "assistant":
            results.append(payload)

    # 整个运行（含工程创建）都在隔离 Home 里：Harness 缓存与会话不会落到用户目录。
    with isolated_home(run_dir):
        project = run_dir / "proj.nproj"
        ops = Operations.create_graph_project(project)
        ops.save_harness_settings(harness_settings(provider))
        source_id = ops.import_source("一致性验收原文", story)
        (run_dir / "source.txt").write_text(story, encoding="utf-8")
        (run_dir / "harness-settings.json").write_text(
            json.dumps(harness_settings(provider), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        bridge = NativeBridge()
        bridge.load_appearance()
        bridge.resultReady.connect(on_result)
        bridge.errorOccurred.connect(lambda kind, message: errors.append(f"{kind}: {message}"))
        bridge.openProject(str(project))
        if not wait_until(bridge, app, lambda: "project" in kinds, 60):
            raise RuntimeError(f"工程没有打开：{errors}")
        kinds.clear()
        started = time.monotonic()
        bridge.runSourceTask(source_id, "", False)
        finished = wait_until(
            bridge, app, lambda: bool(results) and not bridge.pendingKinds(), seconds
        )
        elapsed = time.monotonic() - started
        if not finished:
            errors.append("task_timeout（本次运行被外部预算截断，指标不计入一致性统计）")
        runner = getattr(bridge, "_assistant_runner", None)
        if runner is not None:
            try:
                usage = runner.usage_metadata()
            except Exception:  # noqa: BLE001 - 只做记录，不影响结论
                usage = {}
        bridge.close()
        dump = graph_dump(Operations.open(project))
        calls = chat_calls(project)

    (run_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)[:2_000_000], encoding="utf-8"
    )
    (run_dir / "graph.json").write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "tool-calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "seconds": round(elapsed, 1),
        "timed_out": not finished,
        "errors": "; ".join(errors),
        "event_count": len(dump["events"]),
        "scene_count": len(dump["scenes"]),
        "scene_starts": [row["starts"][:1] for row in dump["scenes"]],
        "event_starts": [row["starts"][:1] for row in dump["events"]],
        "event_names": [row["name"] for row in dump["events"]],
        "counts": dump["counts"],
        "properties": dump["property_records"],
        "duplicate_event_names": dump["duplicate_event_names"],
        "check": dump["check"],
        "tool_calls": [call["tool"] for call in calls],
        "usage": usage,
    }


def jaccard(a: list[int], b: list[int], tolerance: int = 120) -> float:
    """两组事件起点按容差对齐后的重合比例。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    matched = sum(1 for x in a if any(abs(x - y) <= tolerance for y in b))
    return matched / max(len(a), len(b))


def main() -> int:
    parser = argparse.ArgumentParser(description="提取提示词一致性验收")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--provider", default="opencode-go", choices=sorted(PROVIDERS))
    parser.add_argument("--story", type=Path, default=STORY)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    directory = args.output.resolve()
    if directory.exists():
        print(f"输出目录已存在，请换一个：{directory}", file=sys.stderr)
        return 2
    directory.mkdir(parents=True)
    story = args.story.read_text(encoding="utf-8")
    preset = PROVIDERS[args.provider]
    if not os.environ.get(preset["key_env"]):
        print(f"缺少密钥变量 {preset['key_env']}，无法跑真实请求", file=sys.stderr)
        return 2

    # 先起 Qt 应用再导入业务模块：反过来会让 pydantic 与 PySide6 的导入顺序打架。
    from PySide6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    QApplication.instance() or QApplication([])

    from narrative.prompt_defaults import DEFAULT_PROMPTS

    manifest = {
        "label": args.label,
        "started": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": preset["model"],
        "runs": args.runs,
        "external_seconds_per_run": args.seconds,
        "story_sha256_input_chars": len(story),
        "prompt_versions": {key: entry.get("version") for key, entry in DEFAULT_PROMPTS.items()},
        "prompts": {key: entry["text"] for key, entry in DEFAULT_PROMPTS.items()},
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    runs = []
    for index in range(1, args.runs + 1):
        run_dir = directory / f"run{index}"
        summary = run_once(run_dir, story, args.provider, args.seconds)
        runs.append(summary)
        print(f"run{index}: 事件 {summary['event_count']} 个, {summary['seconds']}s, "
              f"tokens={summary['usage'].get('total_tokens')}, errors={summary['errors'] or '-'}")

    starts = [run["event_starts"] for run in runs]
    pairs = [
        (i, j, jaccard(starts[i], starts[j]))
        for i in range(len(starts))
        for j in range(i + 1, len(starts))
    ]
    summary = {
        "label": args.label,
        "runs": runs,
        "event_counts": [run["event_count"] for run in runs],
        "scene_counts": [run["scene_count"] for run in runs],
        "boundary_jaccard": [{"a": i + 1, "b": j + 1, "score": round(score, 3)} for i, j, score in pairs],
        "mean_jaccard": round(sum(score for _, _, score in pairs) / len(pairs), 3) if pairs else None,
        "duplicates": [run["duplicate_event_names"] for run in runs],
        "errors": [run["errors"] for run in runs],
    }
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"事件数 {summary['event_counts']}  平均边界重合 {summary['mean_jaccard']}")
    print(f"证据目录 {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
