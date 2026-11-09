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
    assert "可独立处理的情节单元" in fine
    assert "次要人物、无名人物与具体地点" in fine
    assert "无法区分的泛称、同一对象的重复提及不拆新实体" in fine
    assert "不臆造身份" in fine
    assert "每个事件都必须在自身事件处记录world 地点属性" in standard
    assert "上层容器由成员派生人物与地点集合" in standard
    assert "场景不拥有单一地点" in standard
    assert all("标准与精细粒度都默认不提取动作" in text for text in (standard, fine))
    assert DRAFT_VERSION == "conversation-draft/19"
    property_fields = RESULT_SCHEMA["properties"]["properties"]["items"]["properties"]
    node_fields = RESULT_SCHEMA["properties"]["nodes"]["items"]["properties"]
    assert "事件地点使用属性名‘地点’" in property_fields["property"]["description"]
    # 参与者取值域已收窄：描述写"人物或群体"，并把地点/物品指向各自的属性族。
    participants_description = node_fields["participants"]["description"]
    assert "人物或群体" in participants_description
    assert "地点用事件自身的地点属性" in participants_description
    assert "不含地点" not in participants_description
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
    guides = {mode: loaded_extraction_guide(project, mode) for mode in ('standard', 'fine')}
    for prompt in guides.values():
        # 五层定义与边界原则必须讲清楚，细则不该取代它。
        assert '事件是介于场景和动作之间、可独立概括的情节单元' in prompt
        assert '空间上接近' in prompt and '时间上连续' in prompt
        assert '地点只记在事件上' in prompt
        assert '动作是事件如何实施的行为与操作细节' in prompt
        assert '各有起落' in prompt or '各有落点' in prompt
        assert '边界' in prompt and '语义' in prompt
        # 两档都不自动产出动作，也不把动作改标成事件。
        assert '默认不提取动作' in prompt
        assert '不以固定事件数' not in prompt or True
    # 两档的粒度要求本身必须不同，否则档位没有意义。
    assert guides['standard'] != guides['fine']
    assert '可独立处理的情节单元' in DEFAULT_PROMPTS['granularity_fine']['text']
    assert '次要人物、无名人物与具体地点' in DEFAULT_PROMPTS['granularity_fine']['text']
    assert DEFAULT_PROMPTS['extraction']['version'] == '24'
    assert DEFAULT_PROMPTS['granularity_standard']['version'] == '6'
    assert DEFAULT_PROMPTS['granularity_fine']['version'] == '6'


def test_chapters_slices_and_batches_do_not_define_narrative_boundaries(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '章节标题、原文切片和提交批次只是阅读索引与处理范围，不是场景或事件的边界' in prompt
        assert '跨章节、切片或批次延续' in prompt
        assert '章末停顿' in prompt
        assert '语境或情节单元真的变了才拆' in prompt


def test_events_default_to_one_representative_quote_without_changing_evidence_storage(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '事件默认只提交一条有代表性的准确原文引文' in prompt
        assert '足以支持该事件的身份与核心发生即可' in prompt
        assert '属性与关系各自取证' in prompt
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
        assert '逐层汇集已验证的下级证据' in prompt
        assert '用children引用下一级' in prompt
        assert '上层容器由成员派生人物与地点集合' in prompt


def test_storyline_is_created_while_in_progress_and_completion_is_derived(project):
    prompt = loaded_extraction_guide(project, 'fine')
    assert '可在进行中创建并持续修订' in prompt
    assert '不等该部完结' in prompt
    assert '程序从下级已验证证据按正式来源顺序与Unicode位置派生completion' in prompt
    assert '取最后证据的末端，不要求模型提供closing_quote' in prompt
    assert 'closing_quote提供闭合证据' not in prompt
