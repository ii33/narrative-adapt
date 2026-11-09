"""比较多次提取运行：事件数、场景数、边界重合度、重复命名、工具调用量。

用法（在 narrative-adapt 目录下）：
  .venv/Scripts/python.exe scripts/compare_extraction_runs.py out/old/run1 out/old/run2 ... --output out/compare.json
"""

from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path


def load(run_dir: Path) -> dict:
    graph = json.loads((run_dir / "graph.json").read_text(encoding="utf-8"))
    calls_path = run_dir / "tool-calls.json"
    calls = json.loads(calls_path.read_text(encoding="utf-8")) if calls_path.is_file() else []
    label_file = run_dir / "label.txt"
    label = label_file.read_text(encoding="utf-8").strip() if label_file.is_file() else run_dir.parent.name
    return {"graph": graph, "calls": calls, "dir": run_dir.name, "parent": label}


def jaccard(a: list[int], b: list[int], tolerance: int) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    matched = sum(1 for x in a if any(abs(x - y) <= tolerance for y in b))
    return matched / max(len(a), len(b))


def starts(rows: list[dict]) -> list[int]:
    return sorted(row["starts"][0] for row in rows if row.get("starts"))


def describe(run: dict) -> dict:
    graph = run["graph"]
    return {
        "dir": run["dir"],
        "label": run["parent"],
        "events": len(graph["events"]),
        "scenes": len(graph["scenes"]),
        "persons": graph["counts"].get("person", 0),
        "locations": graph["counts"].get("location", 0),
        "items": graph["counts"].get("item", 0),
        "big_events": graph["counts"].get("big_event", 0),
        "properties": graph["property_records"],
        "duplicates": graph["duplicate_event_names"],
        "tool_calls": len(run["calls"]),
        "event_starts": starts(graph["events"]),
        "scene_starts": starts(graph["scenes"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tolerance", type=int, default=120)
    args = parser.parse_args()

    runs = [load(path) for path in args.runs]
    described = [describe(run) for run in runs]
    pairs = []
    for a, b in combinations(range(len(runs)), 2):
        pairs.append(
            {
                "a": described[a]["dir"],
                "b": described[b]["dir"],
                "same_label": described[a]["label"] == described[b]["label"],
                "event_count_diff": abs(described[a]["events"] - described[b]["events"]),
                "event_start_jaccard": round(
                    jaccard(described[a]["event_starts"], described[b]["event_starts"], args.tolerance), 3
                ),
                "scene_start_jaccard": round(
                    jaccard(described[a]["scene_starts"], described[b]["scene_starts"], args.tolerance), 3
                ),
            }
        )

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    same = [pair for pair in pairs if pair["same_label"]]
    report = {
        "tolerance_chars": args.tolerance,
        "runs": described,
        "pairs": pairs,
        "within_label_event_jaccard": mean([pair["event_start_jaccard"] for pair in same]),
        "within_label_scene_jaccard": mean([pair["scene_start_jaccard"] for pair in same]),
        "event_count_range_by_label": {
            label: {
                "counts": sorted(run["events"] for run in described if run["label"] == label),
            }
            for label in dict.fromkeys(run["label"] for run in described)
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'run':22}{'事件':>5}{'场景':>5}{'人物':>5}{'地点':>5}{'属性':>6}{'工具':>5}")
    for run in described:
        print(f"{run['label'] + '/' + run['dir']:22}{run['events']:>5}{run['scenes']:>5}"
              f"{run['persons']:>5}{run['locations']:>5}{run['properties']:>6}{run['tool_calls']:>5}")
    print(f"\n同一提示词内：事件边界平均重合 {report['within_label_event_jaccard']}，"
          f"场景边界平均重合 {report['within_label_scene_jaccard']}")
    for label, value in report["event_count_range_by_label"].items():
        print(f"  {label}: 事件数 {value['counts']}")
    print(f"明细 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
