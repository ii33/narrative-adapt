"""One isolated real read-only task verifies built-in storyboard usage tips.

Run once into a fresh output directory. External observation defaults are 180
seconds and 40,000 tokens; neither is a product task deadline. No retries.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import accept_screenplay as qa
import accept_tool_loading as loading_qa

TOOL = "read_storyboard_usage_tips"
TEXT = "空房间里有一张桌子。"
TASK = (
    "这是生图使用技巧的只读验收。请先发现并按需加载read_storyboard_usage_tips，"
    "实际调用该工具读取软件内置内容，然后用两句中文说明FLUX原生区域表适合做什么，"
    "以及Qwen和ChatGPT在空间约束上的局限。依据实际读取结果，不猜测已实现能力。"
    "不生成图片，不修改工程、设置或任何文件，不调用写入、生成或导出工具。"
)
PROVIDER_REQUESTS: list[dict] = []


def _actions(ops):
    with sqlite3.connect(ops.project.store.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM actions ORDER BY rowid")]


def seed(ops):
    source_id = ops.import_source("使用技巧验收短文", TEXT)
    screenplay = ops.create_screenplay("生图使用技巧只读样本", reason="隔离验收种子")["screenplay"]
    return {
        "source_id": source_id,
        "screenplay_id": screenplay["id"],
        "source_before": ops.source(source_id),
        "screenplay_before": ops.read_screenplay(screenplay["id"]),
        "properties_before": ops.property_records(),
        "prompt_settings_before": ops.load_prompt_settings(),
        "project_settings_before": ops.load_project_settings(),
        "expected_tips": ops.read_storyboard_usage_tips(),
        "actions_before": _actions(ops),
    }


def assess_loading(requests, steps, sessions):
    catalogs = [{loading_qa.canonical(name) for name in row["tool_names"]} for row in requests]
    def good(name):
        return [index for index, step in enumerate(steps)
            if loading_qa.canonical(step.get("tool")) == name and step.get("ok")]
    discoveries, loads, reads = (good(name) for name in ("list_available_tools", "load_tools", TOOL))
    return {
        "initial_catalog_omits_tips": bool(catalogs) and TOOL not in catalogs[0],
        "later_catalog_exposes_tips": any(TOOL in catalog for catalog in catalogs[1:]),
        "discovered_before_load": bool(discoveries and loads) and min(discoveries) < min(loads),
        "loaded_before_actual_read": bool(loads and reads) and min(loads) < min(reads),
        "same_sdk_session": bool(sessions) and len(set(sessions)) == 1,
    }


def assess_sdk_tips(session_rows, expected):
    """Require the exact nonerror result tied to an actual empty-argument call."""
    messages = [row.get("data", {}).get("message", {}) for row in session_rows]
    call_ids = set()
    for message in messages:
        for block in message.get("content", []) or []:
            if not isinstance(block, dict) or loading_qa.canonical(block.get("name")) != TOOL:
                continue
            args = block.get("arguments", block.get("input", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    continue
            if args == {} and block.get("id"):
                call_ids.add(block["id"])
    responses = []
    for message in messages:
        if message.get("role") != "tool" or message.get("toolCallId") not in call_ids:
            continue
        decoded = []
        for block in message.get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            try:
                decoded.append(json.loads(block.get("text", "")))
            except (json.JSONDecodeError, TypeError):
                pass
        responses.append({
            "tool_call_id": message["toolCallId"],
            "is_error": message.get("isError"),
            "content": message.get("content"),
            "matches_actual_operation_result": any(value == expected for value in decoded),
        })
    return {
        "sdk_received_actual_tips_without_error": any(
            row["is_error"] is not True and row["matches_actual_operation_result"] for row in responses
        ),
    }, responses


def external_abort_reason():
    errors = [error for row in PROVIDER_REQUESTS for error in row.get("sdk_output_schema_errors", [])]
    return (
        "External QA stopped on SDK output schema rejection; no retry: " + errors[0]["tool"]
        if errors else None
    )


def validate(ops, fixture, screenplay, _calls, report):
    from narrative.chat_store import ChatStore
    from narrative.tool_api import TOOLS

    directory = Path(report["audit_directory"])
    audit = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    metadata = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("metadata-*.json"))]
    sessions = [row["session_id"] for row in metadata if row.get("session_id")]
    steps = audit.get("steps", [])
    checks = assess_loading(PROVIDER_REQUESTS, steps, sessions)
    session_files = [path for path in (directory.parents[1] / "home" / "sessions").rglob("session.v4.jsonl")
        if path.parent.name in sessions]
    session_rows = [json.loads(line) for path in session_files
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    sdk_checks, responses = assess_sdk_tips(session_rows, fixture["expected_tips"])
    checks.update(sdk_checks)
    reads = [step for step in steps if loading_qa.canonical(step.get("tool")) == TOOL and step.get("ok")]
    attempted_writes = [step for step in steps
        if (tool := TOOLS.get(loading_qa.canonical(step.get("tool")))) is not None and tool.writes]
    chat = ChatStore(ops.project_info().path).load(audit["chat_id"])
    answers = [message.get("text", "") for message in chat["messages"]
        if message.get("task_id") == audit["task_id"] and message.get("role") == "assistant"
        and message.get("display_kind", "answer") == "answer"]
    checks.update({
        "actual_read_returns_expected_tips": any(step.get("result") == fixture["expected_tips"] for step in reads),
        "tips_still_unchanged": ops.read_storyboard_usage_tips() == fixture["expected_tips"],
        "source_unchanged": ops.source(fixture["source_id"]) == fixture["source_before"],
        "screenplay_unchanged": screenplay == fixture["screenplay_before"],
        "properties_unchanged": ops.property_records() == fixture["properties_before"],
        "project_settings_unchanged": ops.load_project_settings() == fixture["project_settings_before"],
        "prompt_settings_unchanged": ops.load_prompt_settings() == fixture["prompt_settings_before"],
        "no_business_actions_added": _actions(ops) == fixture["actions_before"],
        "no_write_tool_attempted": not attempted_writes,
        "final_answer_present": bool(answers) and bool(answers[-1].strip()),
    })
    report["usage_breakdown"] = loading_qa.usage_breakdown(metadata)
    report["usage_tips_evidence"] = {
        "provider_requests": PROVIDER_REQUESTS,
        "steps": steps,
        "session_ids": sessions,
        "session_files": [str(path) for path in session_files],
        "sdk_responses": responses,
        "final_answers": answers,
        "write_tool_attempts": attempted_writes,
        "manual_review": "Read actual final answer for correct FLUX scope and Qwen/ChatGPT limits. Host expected tips are only an assertion baseline and were not inserted into the model task.",
    }
    return checks


def external_default_arguments(arguments):
    result = list(arguments)
    for option, value in (("--seconds", "180"), ("--token-budget", "40000")):
        if not any(arg == option or arg.startswith(option + "=") for arg in result):
            result.extend([option, value])
    return result



def record_implementation(_ops, fixture, report, directory, _began):
    paths = [
        Path("src/narrative/storyboard_usage_tips.py"),
        Path("src/narrative/storyboard_operations.py"),
        Path("src/narrative/screenplay_tools.py"),
        Path("src/narrative/tool_loading.py"),
        Path("src/narrative/native_bridge.py"),
        Path("src/narrative/qml/StoryboardPage.qml"),
        Path("src/narrative/qml/StoryImageUsageTips.qml"),
        Path(__file__),
    ]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Usage tips implementation is incomplete: " + ", ".join(missing))
    fingerprints = {str(path): qa.digest(path.read_bytes()) for path in paths}
    report["implementation_sha256"].update(fingerprints)
    report["tips_contract"] = {"id": fixture["expected_tips"]["id"], "version": fixture["expected_tips"]["version"]}
    report["real_acceptance_scope"] = "One read-only assistant task; no image model requests."
    (directory / "usage-tips-implementation.json").write_text(json.dumps({
        "tips_contract": report["tips_contract"], "implementation_sha256": fingerprints,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    # Set external defaults without altering the common QA runner or product.
    sys.argv[:] = external_default_arguments(sys.argv)
    qa.seed, qa.TASK, qa.TEXT = seed, TASK, TEXT
    qa.PROMPT_VERSION = "storyboard-usage-tips-readonly/1"
    qa.FIXTURE_ORIGIN = "Fresh isolated project, one short source and an empty screenplay; one actual assistant discovery/load/read task, no generation or writes."
    PROVIDER_REQUESTS.clear()
    with loading_qa.capture_provider_tools(PROVIDER_REQUESTS):
        return qa.main(validate=validate, required_tools=[TOOL], use_writing_target=False,
            prompt_factory=lambda _: TASK, external_abort=external_abort_reason,
            on_started=record_implementation)


if __name__ == "__main__":
    raise SystemExit(main())
