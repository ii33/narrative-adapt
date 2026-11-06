"""Evidence collection separates failed attempts from real successful writes."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from test_screenplay_writing import project as project
from test_screenplay_writing import table

from narrative import assistant, tool_api
from narrative.project_package import layout
from narrative.tool_execution_context import bind_tool_execution_metadata


def evidence(project, tmp_path, monkeypatch, *, commit=True):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    validator = importlib.import_module("accept_readable_tools")
    ops, screenplay_id, segment_id = project
    unit = table(ops, segment_id, [{"plan": {"summary": "甲等待乙。"}}])["units"][0]
    ops.material_add_card("P1", "合成资料")
    ops.save_asset({"category": "material", "title": "合成材质"})
    entry = layout(ops.project_info().path).entry
    context = tool_api.call("read_script_unit_context", {"unit_id": unit["id"]}, project=entry)
    args = {key: context[key] for key in ("expected_plan_revision", "expected_body_revision", "span_fingerprint", "context_fingerprint", "plan_fingerprint")}
    args.update(unit_id=unit["id"], blocks=[{"type": "action", "payload": {"text": "乙推门进来，甲抬头看向门口。"}}], reason="合成工具证据")
    malformed = args | {"blocks": [{"type": "action", "text": "甲把湿伞靠在门边，听见走廊尽头有人停下。"}]}
    before = ops.read_script_unit_context(unit["id"])
    staged_step = assistant._call(entry, "submit_script_unit", malformed)
    staged = staged_step["result"]
    assert staged_step["ok"] is True
    assert staged["status"] == staged["submission_status"] == "staged"
    assert staged["validation_status"] == "invalid"
    assert {tuple(item["path"]) for item in staged["problems"]} >= {("blocks", 0, "payload"), ("blocks", 0, "text")}
    after_staging = ops.read_script_unit_context(unit["id"])
    assert after_staging["expected_body_revision"] == before["expected_body_revision"]
    assert after_staging["unit"]["blocks"] == before["unit"]["blocks"]
    audit = tmp_path / "synthetic-task"
    audit.mkdir()
    calls = [{"name": "submit_script_unit", "arguments": json.dumps(malformed)}]
    steps = [staged_step]
    if commit:
        with bind_tool_execution_metadata({"task_id": audit.name, "manifest_ref": str(audit / "writing-task.json")}):
            committed_step = assistant._call(entry, "submit_script_unit", args)
        result = committed_step["result"]
        assert committed_step["ok"] is True
        assert result["status"] == "accepted" and result["submission_status"] == "committed"
        assert result["validation_status"] == "valid"
        calls.append({"name": "submit_script_unit", "arguments": json.dumps(args)})
        steps.append(committed_step)
    fixture = {"person_id": "P1", "unit_id": unit["id"], "screenplay_id": screenplay_id}
    return validator, ops, fixture, calls, steps, audit, before, after_staging


def test_same_task_staged_content_does_not_count_as_second_commit(project, tmp_path, monkeypatch):
    validator, ops, fixture, calls, steps, audit, _before, _after = evidence(project, tmp_path, monkeypatch)
    (audit / "task.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    report = {"audit_directory": str(audit)}
    checks = validator.validate(ops, fixture, ops.read_screenplay(fixture["screenplay_id"]), calls, report)
    assert all(checks.values()), checks
    assert len(report["readable_id_evidence"]["raw_submit_attempts"]) == 2
    assert len(report["readable_id_evidence"]["successful_submit_arguments"]) == 1
    assert len(report["readable_id_evidence"]["staged_submit_attempts"]) == 1
    assert not report["readable_id_evidence"]["rejected_submit_attempts"]
    assert not report["readable_id_evidence"]["unclassified_submit_attempts"]


def test_staging_does_not_change_saved_body(project, tmp_path, monkeypatch):
    _validator, _ops, _fixture, _calls, steps, _audit, before, after = evidence(
        project, tmp_path, monkeypatch, commit=False)
    assert len(steps) == 1 and steps[0]["result"]["submission_status"] == "staged"
    assert after["expected_body_revision"] == before["expected_body_revision"]
    assert after["unit"]["blocks"] == before["unit"]["blocks"]


@pytest.mark.parametrize("unexpected_result", [
    {"status": "accepted", "submission_status": "committed", "validation_status": "invalid"},
    {"status": "staged", "submission_status": "staged", "validation_status": "valid"},
    None,
])
def test_unexpected_submit_result_is_reported_and_fails_acceptance(
        project, tmp_path, monkeypatch, unexpected_result):
    validator, ops, fixture, calls, steps, audit, _before, _after = evidence(project, tmp_path, monkeypatch)
    unexpected = {"tool": "submit_script_unit", "args": steps[1]["args"], "ok": True,
                  "result": unexpected_result}
    (audit / "task.json").write_text(json.dumps({"steps": [*steps, unexpected]}), encoding="utf-8")
    report = {"audit_directory": str(audit)}
    checks = validator.validate(ops, fixture, ops.read_screenplay(fixture["screenplay_id"]), calls, report)
    assert not checks["all_submit_steps_classified"]
    assert not all(checks.values())
    evidence_rows = report["readable_id_evidence"]
    assert evidence_rows["unclassified_submit_attempts"] == [unexpected]
    assert len(evidence_rows["successful_submit_arguments"]) == 1
    assert len(evidence_rows["staged_submit_attempts"]) == 1


def test_duplicate_committed_submit_fails_acceptance(project, tmp_path, monkeypatch):
    validator, ops, fixture, calls, steps, audit, _before, _after = evidence(project, tmp_path, monkeypatch)
    entry = layout(ops.project_info().path).entry
    context = tool_api.call("read_script_unit_context", {"unit_id": fixture["unit_id"]}, project=entry)
    duplicate_args = {key: context[key] for key in (
        "expected_plan_revision", "expected_body_revision", "span_fingerprint", "context_fingerprint", "plan_fingerprint")}
    duplicate_args.update(unit_id=fixture["unit_id"],
        blocks=[{"type": "action", "payload": {"text": "乙把钥匙放在桌上，甲没有伸手去接。"}}],
        reason="重复提交合成正文")
    with bind_tool_execution_metadata({"task_id": audit.name, "manifest_ref": str(audit / "writing-task.json")}):
        duplicate_step = assistant._call(entry, "submit_script_unit", duplicate_args)
    duplicate = duplicate_step["result"]
    assert duplicate_step["ok"] is True
    assert duplicate["status"] == "accepted" and duplicate["submission_status"] == "committed"
    assert duplicate["validation_status"] == "valid"
    calls.append({"name": "submit_script_unit", "arguments": json.dumps(duplicate_args)})
    steps.append(duplicate_step)
    (audit / "task.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    report = {"audit_directory": str(audit)}
    checks = validator.validate(ops, fixture, ops.read_screenplay(fixture["screenplay_id"]), calls, report)
    assert checks["all_submit_steps_classified"]
    assert not checks["exact_target_submit"]
    assert not all(checks.values())
    evidence_rows = report["readable_id_evidence"]
    assert len(evidence_rows["successful_submit_arguments"]) == 2
    assert not evidence_rows["unclassified_submit_attempts"]


def test_final_body_alone_cannot_replace_successful_tool_evidence(project, tmp_path, monkeypatch):
    validator, ops, fixture, calls, steps, audit, _before, _after = evidence(project, tmp_path, monkeypatch)
    (audit / "task.json").write_text(json.dumps({"steps": steps[:1]}), encoding="utf-8")
    checks = validator.validate(ops, fixture, ops.read_screenplay(fixture["screenplay_id"]), calls, {"audit_directory": str(audit)})
    assert not checks["exact_target_submit"]
    assert not checks["submitted_body_matches_saved"]
    assert not checks["provenance_internal"]


@pytest.mark.parametrize("missing_args", [None, "unit_id"])
def test_missing_committed_arguments_fails_closed(project, tmp_path, monkeypatch, missing_args):
    validator, ops, fixture, calls, steps, audit, _before, _after = evidence(project, tmp_path, monkeypatch)
    malformed = dict(steps[1])
    if missing_args is None:
        malformed.pop("args")
    else:
        malformed["args"] = {key: value for key, value in malformed["args"].items() if key != missing_args}
    (audit / "task.json").write_text(json.dumps({"steps": [*steps, malformed]}), encoding="utf-8")
    report = {"audit_directory": str(audit)}
    checks = validator.validate(ops, fixture, ops.read_screenplay(fixture["screenplay_id"]), calls, report)
    assert not checks["all_submit_steps_classified"]
    assert report["readable_id_evidence"]["unclassified_submit_attempts"] == [malformed]


def test_staged_only_with_empty_body_fails_without_crashing(project, tmp_path, monkeypatch):
    validator, ops, fixture, calls, steps, audit, _before, _after = evidence(project, tmp_path, monkeypatch, commit=False)
    (audit / "task.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    report = {"audit_directory": str(audit)}
    checks = validator.validate(ops, fixture, ops.read_screenplay(fixture["screenplay_id"]), calls, report)
    assert not checks["one_real_action"] and not checks["readable_scene_and_block"]
    assert not checks["exact_target_submit"]
    assert not report["readable_id_evidence"]["successful_submit_arguments"]
