"""Task-local instructions reflect the saved project detail without action auto-expansion."""
import pytest
from agent_fixtures import project as project

from narrative.assistant import _first_prompt
from narrative.extraction_draft import DRAFT_VERSION, RESULT_SCHEMA


def loaded_extraction_guide(project, mode):
    from narrative.tool_loading import tool_guidance
    project.save_project_settings(project.load_project_settings() | {'extraction_granularity': mode})
    result = tool_guidance(project, ['submit_extraction'])
    assert result['extraction_granularity'] == mode
    return '\n'.join(row['text'] for row in result['guides'])


def test_standard_and_fine_are_distinct_but_both_leave_actions_opt_in(project):
    standard = loaded_extraction_guide(project, "standard")
    fine = loaded_extraction_guide(project, "fine")
    assert "当前工程提取粒度：standard" in _first_prompt('提取', extraction_granularity='standard')
    assert "当前工程提取粒度：fine" in _first_prompt('提取', extraction_granularity='fine')
    assert "保留场景内部可独立处理的情节单元" in fine
    assert "次要、无名人物和具体地点" in fine
    assert "无法区分的泛称或重复提及不拆成新实体" in fine
    assert "不臆造身份" in fine
    assert "每个事件都必须在自身事件处记录world“地点”属性" in standard
    assert "上层容器由成员派生人物与地点，不重复写回" in standard
    assert all("标准与精细粒度都默认不提取动作" in text for text in (standard, fine))
    assert DRAFT_VERSION == "conversation-draft/19"
    property_fields = RESULT_SCHEMA["properties"]["properties"]["items"]["properties"]
    node_fields = RESULT_SCHEMA["properties"]["nodes"]["items"]["properties"]
    assert "事件地点使用属性名‘地点’" in property_fields["property"]["description"]
    assert "不含地点" in node_fields["participants"]["description"]
    assert node_fields['location']['type'] == 'string' and "locations" not in node_fields
    with pytest.raises(ValueError, match="提取粒度"):
        _first_prompt("提取", extraction_granularity="unknown")


def test_operations_accepts_event_location_reference_and_secondary_people(project):
    text = "旅客甲和旅客乙跟着无名侍者走进客栈后院。"
    source_id = project.import_source("精细地点和次要人物", text)
    draft = {"nodes": [
        {"type": "person", "name": "旅客甲", "quotes": [text]},
        {"type": "person", "name": "旅客乙", "quotes": [text]},
        {"type": "person", "name": "无名侍者", "quotes": ["无名侍者"]},
        {"type": "location", "name": "客栈后院", "quotes": ["客栈后院"]},
        {"type": "scene", "name": "进入客栈后院", "quotes": [text]},
        {"type": "event", "name": "跟随侍者进入后院", "scene": "@5",
         "participants": ["@1", "@2", "@3"], "quotes": [text]},
    ], "properties": [{
        "node": "@6", "op": "set", "property": "地点", "value": "@4", "value_type": "node",
        "scope": "world", "at_event": "@6", "quotes": ["客栈后院"],
    }]}
    accepted = project.submit_extraction(source_id, draft)
    assert accepted["status"] == "accepted", accepted

    nodes = {row["id"]: row for row in project.graph_snapshot()["nodes"]}
    people = {row["name"]: row["id"] for row in nodes.values() if row["node_type"] == "person"}
    location = next(row for row in nodes.values() if row["node_type"] == "location")
    event = next(row for row in nodes.values() if row["node_type"] == "event")
    assert set(event["participants"]) == set(people.values())
    assert len(people) == 3 and "无名侍者" in people

    before = project.property_records()
    location_properties = [row for row in before
                           if row.get("kind") == "property_change" and row.get("property_id") == "地点"]
    assert len(location_properties) == 1
    location_property = location_properties[0]
    location_definition = next(row for row in before if row.get("kind") == "property_definition"
                              and row.get("id") == "地点")
    assert (location_property["node_id"], location_property["property_id"],
            location_property["value"], location_definition["value_type"],
            location_property["scope"], location_property["event_id"]) == (
                event["id"], "地点", location["id"], "node", "world", event["id"])
    assert location_property["disclosure"]["start"] == text.index("客栈后院")
    summary = project.property_node_summary(event["id"])
    assert next(row for row in summary["properties"] if row["property_id"] == "地点")["category"] == "基本信息"
    assert project.property_records() == before


def test_both_modes_balance_events_between_scenes_and_actions(project):
    from narrative.prompt_defaults import DEFAULT_PROMPTS
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '事件是介于场景和动作之间、可独立概括的情节单元' in prompt
        assert '场景是连续时空或叙事语境的容器' in prompt
        assert '动作是事件如何实施的行为与操作细节' in prompt
        assert '同一场景、同一总体目标可以包含多个事件' in prompt
        assert '不要求更换时间地点、另起新目标或引发后续独立展开才允许分开' in prompt
        assert '事件自身的完整性' in prompt
        assert '既不自动各成事件，也不自动并入前后事件' in prompt
        assert '起因、推进和落点' in prompt
        assert '不因服务同一总目标而压成一个事件' in prompt
        assert '双向检查粒度：过细时合并' in prompt
        assert '过粗时拆开' in prompt
        assert '不以固定事件数、事件与场景比例、词表或字数配额套用' in prompt
        assert '不新增固定提取分轮，不要求额外工具、重复读取或preview' in prompt
        assert '不能把本应按需展开的动作改标为E' in prompt
        assert '不按动作词机械排除' in prompt
        assert '边界由模型结合原文语义判断' in prompt
        assert '先合成一个候选再提交' not in prompt
        assert '只有它引发后续独立展开' not in prompt
        assert '整理包装、送达、收件确认、收拾离开' not in prompt
    fine = DEFAULT_PROMPTS['granularity_fine']['text']
    assert '精细不等于把连续动作链拆成更多E' in fine
    assert '每个属性变化' in fine
    assert '次要、无名人物和具体地点' in fine
    assert '可独立处理的情节单元' in fine
    assert DEFAULT_PROMPTS['extraction']['version'] == '20'
    assert DEFAULT_PROMPTS['granularity_standard']['version'] == '4'
    assert DEFAULT_PROMPTS['granularity_fine']['version'] == '4'



def test_chapters_slices_and_batches_do_not_define_narrative_boundaries(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '章节标题、原文切片和提交批次只是阅读索引与处理范围，不是场景或事件的边界' in prompt
        assert '不能把章末停顿当作完整事件的落点' in prompt
        assert '可以跨章节、切片或批次延续' in prompt
        assert '不能因换章、读完切片或一批已入库就新建同义节点或强行收尾' in prompt
        assert '复用已查证的existing_id' in prompt
        assert '保留已有证据与子项' in prompt
        assert '新增引文仍须属于本次提交范围' in prompt
        assert '跨章也不意味着必须合并' in prompt
        assert '实际时空、语境或完整情节单元发生变化时，仍按原文语义分别建场景或事件' in prompt


def test_events_default_to_one_representative_quote_without_changing_evidence_storage(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '事件默认只提交一条有代表性的准确原文引文' in prompt
        assert '足以支持该事件的身份与核心发生即可' in prompt
        assert '不为起因、过程、结果分别罗列引文' in prompt
        assert '不要求章界两侧各引用一条' in prompt
        assert '事件的完整性和情节边界独立于引文数量' in prompt
        assert '不能用事件的一条引文替代其他事实的取证' in prompt
        assert '不删除历史证据，也不把一条引文设为结构校验上限' in prompt
    quote_schema = RESULT_SCHEMA['properties']['nodes']['items']['properties']['quotes']
    assert 'maxItems' not in quote_schema
    assert '一条' in quote_schema['description']


def test_default_remains_fine_and_independently_consequential_action_can_be_an_event(project):
    before = project.load_project_settings()['extraction_granularity']
    assert before == 'fine'
    text = '甲销毁唯一副本，计划因此无法继续。'
    sid = project.import_source('独立后果', text)
    result = project.submit_extraction(sid, {'nodes': [
        {'type': 'person', 'name': '甲', 'quotes': [text]},
        {'type': 'scene', 'name': '计划中断', 'quotes': [text]},
        {'type': 'event', 'name': '销毁副本使计划中断', 'scene': '@2', 'participants': ['@1'], 'quotes': [text]},
    ], 'properties': [{'node': '@3', 'property': '地点', 'value_type': 'either',
        'scope': 'world', 'at_event': '@3', 'op': 'unknown', 'quotes': [text]}]})
    assert result['status'] == 'accepted'
    assert [n['node_type'] for n in project.graph_snapshot()['nodes']].count('event') == 1
    assert not any(n['node_type'] == 'action' for n in project.graph_snapshot()['nodes'])
    assert project.load_project_settings()['extraction_granularity'] == before


def test_containers_derive_evidence_instead_of_requesting_model_quotes(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '场景（S）、大事件（B）和主要故事（T）不由模型填写quotes或closing_quote' in prompt
        assert '用children引用下一级，或在下一级填写父级引用' in prompt
        assert '逐层汇集已验证的下级证据，派生上层证据与阅读起点' in prompt
        assert '引用已有下级时复用其ID和已验证证据' in prompt
        assert '不要求把这些旧证据重新抄入本批范围' in prompt
        assert '不以给上层补quotes绕过' in prompt
        assert '事件默认只提交一条有代表性的准确原文引文' in prompt
        assert '本批新事件、动作、人物及其他实体仍须自身准确证据' in prompt
        assert '不能用事件的一条引文替代其他事实的取证' in prompt


def test_storyline_closure_is_semantic_and_completion_is_derived(project):
    prompt = loaded_extraction_guide(project, 'fine')
    assert '模型确认完整大篇章真正结束后总结创建' in prompt
    assert '程序从下级已验证证据按正式来源顺序与Unicode位置派生completion' in prompt
    assert '取最后证据的末端，不要求模型提供closing_quote' in prompt
    assert '不能因本批、切片或章节结束就闭合主要故事' in prompt
    assert 'closing_quote提供闭合证据' not in prompt
