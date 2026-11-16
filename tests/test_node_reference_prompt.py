"""The task prompt prefers formal typed IDs and batch-local names, never position indexes."""
import json
import re
from pathlib import Path

from agent_fixtures import project as project

from narrative import assistant, tool_api
from narrative.extraction_draft import RESULT_SCHEMA


def test_reference_prompt_is_concise_and_does_not_require_repeating_existing_nodes(project):
    from narrative.tool_loading import tool_guidance
    for detail in ("standard", "fine"):
        project.save_project_settings(project.load_project_settings() | {'extraction_granularity': detail})
        initial=assistant._first_prompt("请补充已知人物的属性。", extraction_granularity=detail)
        prompt='\n'.join(row['text'] for row in tool_guidance(project, ['submit_extraction'])['guides'])
        assert "@1" not in prompt and "@2" not in prompt and "nodes数组从1开始" not in prompt
        assert "一律用正式 ID" in prompt and "暂存时" in prompt and "node_ids" in prompt
        # 行号还有，但它只说明「第几行」，不再兼任引用写法。
        assert "行号只是第几行，不是节点引用" in prompt
        assert "P1" in prompt and "E2" in prompt
        assert "已有节点不必重列nodes或引文" in prompt
        assert "nodes:[]" in prompt and "properties直接引用P1/E2" in prompt
        assert "同名要改名或直接用 ID" in prompt
        assert "程序不按名字猜首个匹配" in prompt
        assert "用户说：请补充已知人物的属性。" in initial
    assert "不写@位置索引" in tool_api.TOOLS["submit_extraction"].params["result"]["description"]


def test_documented_reference_example_uses_only_current_draft_fields():
    document=Path("docs/features/AGENT_API.md").read_text(encoding="utf-8")
    examples=[json.loads(value) for value in re.findall(r"```json\n(.*?)\n```",document,re.S)]
    draft=next(value for value in examples if "nodes" in value and "properties" in value)
    allowed_nodes=RESULT_SCHEMA["properties"]["nodes"]["items"]["properties"]
    allowed_properties=RESULT_SCHEMA["properties"]["properties"]["items"]["properties"]
    assert all(set(row) <= set(allowed_nodes) for row in draft["nodes"])
    assert all(set(row) <= set(allowed_properties) for row in draft["properties"])
    by_type = {row["type"]: row for row in draft["nodes"]}
    person, phase, scene, event = (by_type["person"], by_type["big_event"],
                                  by_type["scene"], by_type["event"])
    # 示例只用两种写法：本批节点的名称、工程已有的正式 ID；不再出现 @ 位置编号。
    assert scene["big_event"] == phase["name"]
    assert event["scene"] == scene["name"]
    assert event["participants"] == [person["name"], "P2"]
    assert draft["properties"][0]["node"] == person["name"]
    assert draft["properties"][0]["at_event"] == event["name"]
    for row in draft["nodes"] + draft["properties"]:
        for value in row.values():
            assert not (isinstance(value, str) and value.startswith("@")), row

    existing=next(value for value in examples if value.get("nodes") == [])
    assert existing["properties"][0]["node"] == "P1"
    assert existing["properties"][0]["at_event"] == "E1"
