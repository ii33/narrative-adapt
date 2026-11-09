"""验收脚本的收尾统计可离线重算：不调模型，只用已落盘的 run 目录。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "accept_extraction_consistency.py"


def write_run(directory: Path, *, starts: list[int], scene_starts: list[int]) -> None:
    directory.mkdir(parents=True)
    graph = {
        "counts": {"event": len(starts), "scene": len(scene_starts)},
        "nodes": len(starts) + len(scene_starts),
        "property_records": 5,
        "events": [{"id": f"E{index}", "name": f"事件{index}", "starts": [start]}
                   for index, start in enumerate(starts)],
        "scenes": [{"id": f"S{index}", "name": f"场景{index}", "starts": [start]}
                   for index, start in enumerate(scene_starts)],
        "duplicate_event_names": {},
        "check": {"error": 0, "warning": 0, "info": 0},
    }
    (directory / "graph.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    (directory / "results.json").write_text(json.dumps([{"kind": "assistant", "text": "ok"}], ensure_ascii=False),
                                           encoding="utf-8")
    (directory / "tool-calls.json").write_text(json.dumps([{"tool": "submit_extraction", "arguments": {}}],
                                                         ensure_ascii=False), encoding="utf-8")


def test_summary_is_rebuilt_offline_and_matches_the_boundaries(tmp_path):
    """防止收尾统计再因为「事件起点是嵌套列表」而崩掉：只读产物，退出码必须 0。"""
    output = tmp_path / "v-test"
    write_run(output / "run1", starts=[0, 500], scene_starts=[0])
    write_run(output / "run2", starts=[0, 900], scene_starts=[0])

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), "--summarize-only", "--label", "v-test"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["label"] == "v-test"
    assert summary["event_counts"] == [2, 2]
    assert summary["scene_counts"] == [1, 1]
    assert [run["event_starts"] for run in summary["runs"]] == [[0, 500], [0, 900]]
    # 起点 0 在容差（120 字）内对齐、500 与 900 差 400 不对齐 → 1/2。
    assert summary["boundary_jaccard"] == [{"a": 1, "b": 2, "score": 0.5}]
    assert summary["mean_jaccard"] == 0.5
    assert summary["duplicates"] == [{}, {}]
    assert summary["errors"] == ["", ""]
    assert all(run["event_count"] == 2 for run in summary["runs"])


def test_run_once_shaped_summaries_do_not_break_the_boundary_math():
    """run_once 产出的摘要（起点是标量、可能缺证据为 None）也要能被同样地汇总。"""
    import runpy

    module = runpy.run_path(str(SCRIPT))
    runs = [
        {"event_count": 2, "scene_count": 1,
         "event_starts": [0, 500], "scene_starts": [0],
         "duplicate_event_names": {}, "errors": ""},
        {"event_count": 1, "scene_count": 1,
         "event_starts": [None], "scene_starts": [0],
         "duplicate_event_names": {"甲中等候": 2}, "errors": ""},
    ]
    summary = module["build_summary"](runs, "shape")
    assert summary["event_counts"] == [2, 1]
    assert summary["boundary_jaccard"] == [{"a": 1, "b": 2, "score": 0.0}]
    assert summary["duplicates"] == [{}, {"甲中等候": 2}]
    assert module["first_start"]({"starts": []}) is None
    assert module["first_start"]({"starts": [40, 12]}) == 12
