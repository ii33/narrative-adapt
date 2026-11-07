"""Offline checks for the minimal real read-only usage-tips acceptance."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("usage_tips_acceptance", scripts / "accept_storyboard_usage_tips.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sdk_rows(value, *, error=False, call_id="tips-call", args="{}"):
    return [
        {"data": {"message": {"role": "assistant", "content": [
            {"type": "tool-call", "id": "tips-call", "name": "read_storyboard_usage_tips", "arguments": args}]}}},
        {"data": {"message": {"role": "tool", "toolCallId": call_id, "isError": error,
            "content": [{"type": "text", "text": json.dumps(value)}]}}},
    ]


def test_requires_actual_exact_nonerror_sdk_response(acceptance):
    expected = {"id": "spatial-layout-generation", "version": "1", "sections": [{"id": "flux"}]}
    checks, responses = acceptance.assess_sdk_tips(sdk_rows(expected), expected)
    assert all(checks.values())
    assert responses[0]["matches_actual_operation_result"]
    for rows in [sdk_rows(expected, error=True), sdk_rows(expected, call_id="unrelated"),
                 sdk_rows({"id": expected["id"]}), sdk_rows(expected, args='{"unit_id":"invented"}'),
                 sdk_rows(expected, args="not-json")]:
        checks, _ = acceptance.assess_sdk_tips(rows, expected)
        assert not all(checks.values())


def test_sdk_schema_error_cannot_masquerade_as_tool_success(acceptance):
    expected = {"id": "spatial-layout-generation"}
    checks, _ = acceptance.assess_sdk_tips(sdk_rows("Error: invalid output schema", error=True), expected)
    assert not all(checks.values())


def test_loading_requires_discovery_then_load_then_read_in_same_session(acceptance):
    requests = [{"tool_names": ["list_available_tools", "load_tools"]},
                {"tool_names": ["read_storyboard_usage_tips"]}]
    steps = [{"tool": "list_available_tools", "ok": True}, {"tool": "load_tools", "ok": True},
             {"tool": "read_storyboard_usage_tips", "ok": True}]
    assert all(acceptance.assess_loading(requests, steps, ["session", "session"]).values())
    checks = acceptance.assess_loading(requests, steps[::-1], ["session", "new-session"])
    assert not checks["loaded_before_actual_read"]
    assert not checks["same_sdk_session"]


def test_read_must_not_be_initially_exposed(acceptance):
    checks = acceptance.assess_loading([{"tool_names": ["read_storyboard_usage_tips"]}], [], [])
    assert not checks["initial_catalog_omits_tips"]
    assert not checks["same_sdk_session"]


def test_external_defaults_do_not_override_explicit_values(acceptance):
    assert acceptance.external_default_arguments(["run", "--output", "isolated"]) == [
        "run", "--output", "isolated", "--seconds", "180", "--token-budget", "40000"]
    explicit = ["run", "--seconds=20", "--token-budget", "1000"]
    assert acceptance.external_default_arguments(explicit) == explicit


def test_schema_rejection_aborts_only_qa_without_retry(acceptance):
    acceptance.PROVIDER_REQUESTS.clear()
    assert acceptance.external_abort_reason() is None
    acceptance.PROVIDER_REQUESTS.append({"sdk_output_schema_errors": [{"tool": "read_storyboard_usage_tips"}]})
    assert "no retry" in acceptance.external_abort_reason()


def test_isolated_seed_and_common_runner_reads_do_not_change_business(acceptance, tmp_path):
    from narrative.operations import Operations

    ops = Operations.create_graph_project(tmp_path / "tips-readonly.nproj")
    fixture = acceptance.seed(ops)
    assert fixture["source_before"]["text"] == acceptance.TEXT
    assert fixture["expected_tips"] == ops.read_storyboard_usage_tips()
    assert ops.read_screenplay(fixture["screenplay_id"]) == fixture["screenplay_before"]
    ops.check_screenplay(fixture["screenplay_id"])
    ops.export_screenplay(fixture["screenplay_id"], "text")
    assert acceptance._actions(ops) == fixture["actions_before"]


def test_records_actual_tips_and_implementation_versions(acceptance, tmp_path):
    report = {"implementation_sha256": {}}
    fixture = {"expected_tips": {"id": "spatial-layout-generation", "version": "1"}}
    acceptance.record_implementation(None, fixture, report, tmp_path, 0)
    saved = json.loads((tmp_path / "usage-tips-implementation.json").read_text(encoding="utf-8"))
    assert saved["tips_contract"] == fixture["expected_tips"]
    for path in ["src/narrative/storyboard_usage_tips.py", "src/narrative/storyboard_operations.py",
                 "src/narrative/screenplay_tools.py", "src/narrative/tool_loading.py",
                 "src/narrative/qml/StoryImageUsageTips.qml"]:
        assert saved["implementation_sha256"][str(Path(path))] == acceptance.qa.digest(Path(path).read_bytes())
