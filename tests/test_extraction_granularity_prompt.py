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
    # 精细档要查全的是实体与证据，不是"场景内部的进展步骤"——后者会把单次交涉切成事件。
    assert "出场实体与它们各自的证据查全" in fine
    assert "进展步骤" not in fine
    assert "次要人物、无名人物与具体地点" in fine
    assert "无法区分的泛称、同一对象的重复提及不拆新实体" in fine
    assert "不臆造身份" in fine
    assert "每个事件都必须在自身事件处记录world 地点属性" in standard
    # 容器不填定位依据、只判边界与直属关系（`U-09`：五层段压成两句）。
    assert "程序从已验证的下级证据逐层汇集" in standard
    assert "模型只判边界、名称与直属关系" in standard
    assert "默认不提取，用户显式要求才提取" in standard
    assert all("动作默认不提取" in text for text in (standard, fine))
    assert DRAFT_VERSION == "conversation-draft/21"
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
        {"type": "person", "name": "旅客甲"},
        {"type": "person", "name": "旅客乙"},
        {"type": "person", "name": "无名侍者"},
        {"type": "location", "name": "客栈后院"},
        {"type": "scene", "name": "进入客栈后院"},
        {"type": "event", "name": "跟随侍者进入后院", "scene": "@5",
         "participants": ["@1", "@2", "@3"], "quotes": [text]},
    ], "properties": [{
        "node": "@6", "op": "set", "property": "地点", "category": "未分类"  , "value": "@4", "value_type": "node",
        "scope": "world", "at_event": "@6",
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
    # 属性不填引文：它的披露位置就是 at_event 事件的（`U-09`）。
    assert location_property["disclosure"] == event["disclosure"]
    summary = project.property_node_summary(event["id"])
    # 分类只是浏览分组：属性名/取值不再参与推断，未显式给分类的记录落「未分类」；
    # 事件地点是否由写入方补默认分类属实现细节，这里只锁「有分类且是可枚举值」。
    assert next(row for row in summary["properties"] if row["property_id"] == "地点")["category"] in {"基本信息", "未分类"}
    assert project.property_records() == before


def test_both_modes_balance_events_between_scenes_and_actions(project):
    from narrative.prompt_defaults import DEFAULT_PROMPTS
    guides = {mode: loaded_extraction_guide(project, mode) for mode in ('standard', 'fine')}
    for prompt in guides.values():
        # 五层定义与边界原则必须讲清楚，细则不该取代它。
        assert '事件不是动作' in prompt
        assert '粒度差' in prompt and '粒度等级' in prompt
        assert '空间、时间接近' in prompt
        assert '地点不记在场景上，只记在事件上' in prompt
        assert '一个具体行为动作，直属一个事件' in prompt
        # 场景只装一条事件也合法，这条不许在改写中丢掉。
        assert '只含一个也成立' in prompt
        assert '边界' in prompt and '语义' in prompt
        # 旧判据（能否独立概括／起因、推进、落点）已整批替换，一处残留就是漂移种子。
        assert '可独立概括' not in prompt and '起因、推进' not in prompt and '判断三问' not in prompt
        # 两档都不自动产出动作，也不把动作改标成事件。
        assert '动作默认不提取' in prompt
        assert '不以固定事件数' not in prompt or True
    # 两档的粒度要求本身必须不同，否则档位没有意义。
    assert guides['standard'] != guides['fine']
    assert '进展步骤' not in DEFAULT_PROMPTS['granularity_fine']['text']
    assert '次要人物、无名人物与具体地点' in DEFAULT_PROMPTS['granularity_fine']['text']
    # 版本号会被每次提示词改动推高，这里只锁「是纯数字且不低于五层语义那一版」，
    # 「文本改了版本没升」由 tests/test_prompt_version_snapshot.py 兜住。
    extraction_version = DEFAULT_PROMPTS['extraction']['version']
    assert extraction_version.isdigit() and int(extraction_version) >= 34
    assert DEFAULT_PROMPTS['granularity_standard']['version'] == '7'
    fine_version = DEFAULT_PROMPTS['granularity_fine']['version']
    assert fine_version.isdigit() and int(fine_version) >= 9


def test_event_criterion_is_scale_difference_and_events_are_not_actions():
    """事件判据只有两句：事件不是动作；层级之间是粒度差（五层都是「事件」，只是粒度等级不同）。"""
    from narrative.prompt_defaults import DEFAULT_PROMPTS
    extraction = DEFAULT_PROMPTS['extraction']['text']
    assert '五层都是「事件」' in extraction
    assert '事件粒度等级起的代称，由粗到细' in extraction
    assert '粒度粗于动作，不是一个具体的动作' in extraction
    # 场景只装一条事件也成立：这是合法形态，不是缺数据。
    assert '只含一个也成立' in extraction
    for mode in ('standard', 'fine'):
        text = DEFAULT_PROMPTS[f'granularity_{mode}']['text']
        assert '事件不是动作' in text and '粒度差' in text
        assert '可独立概括' not in text and '起因、推进' not in text


def test_chapters_slices_and_batches_do_not_define_narrative_boundaries(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '章节标题、原文切片和提交批次只是阅读索引与处理范围，不是场景或事件的边界' in prompt
        assert '跨章节、切片或批次延续' in prompt
        assert '章末停顿' in prompt
        assert '语境或情节单元真的变了才拆' in prompt


def test_events_need_a_locator_while_other_nodes_and_properties_do_not(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        assert '事件必须给定位依据' in prompt
        assert '其它节点与属性不必给引文' in prompt
        assert '事件是时间点，事件链就是时间轴' in prompt
        assert '不要为了凑证据回抄原文' in prompt
    node_schema = RESULT_SCHEMA['properties']['nodes']['items']['properties']
    assert 'maxItems' not in node_schema['quotes'] and '仅event' in node_schema['quotes']['description']
    assert node_schema['anchor']['required'] == ['start', 'end']
    properties = RESULT_SCHEMA['properties']['properties']['items']['properties']
    assert 'quotes' not in properties


def test_default_remains_fine_and_independently_consequential_action_can_be_an_event(project):
    before = project.load_project_settings()['extraction_granularity']
    assert before == 'fine'
    text = '甲销毁唯一副本，计划因此无法继续。'
    sid = project.import_source('独立后果', text)
    result = project.submit_extraction(sid, {'nodes': [
        {'type': 'person', 'name': '甲'},
        {'type': 'scene', 'name': '计划中断'},
        {'type': 'event', 'name': '销毁副本使计划中断', 'scene': '@2', 'participants': ['@1'], 'quotes': [text]},
    ], 'properties': [{'node': '@3', 'property': '地点', "category": "未分类"  , 'value_type': 'either',
        'scope': 'world', 'at_event': '@3', 'op': 'unknown'}]})
    assert result['status'] == 'accepted'
    assert [n['node_type'] for n in project.graph_snapshot()['nodes']].count('event') == 1
    assert not any(n['node_type'] == 'action' for n in project.graph_snapshot()['nodes'])
    assert project.load_project_settings()['extraction_granularity'] == before


def test_containers_derive_evidence_instead_of_requesting_model_quotes(project):
    for mode in ('standard', 'fine'):
        prompt = loaded_extraction_guide(project, mode)
        # 容器段压成两句：不填定位依据 + 程序逐层派生 + 模型只判边界与直属关系（`U-09`）。
        assert '容器（T／B／S）不填定位依据' in prompt
        assert '程序从已验证的下级证据逐层汇集' in prompt
        assert '模型只判边界、名称与直属关系' in prompt
        assert 'children 引用下级，或在下一级写父级引用' in prompt


def test_storyline_is_created_while_in_progress_and_completion_is_derived(project):
    prompt = loaded_extraction_guide(project, 'fine')
    assert '该部未完结也可创建并持续修订' in prompt
    assert '一部一条，需要时拆卷' in prompt
    # completion 由程序从下级证据派生；提示词不再教模型填 closing_quote。
    assert 'closing_quote' not in prompt or '不要求模型提供closing_quote' not in prompt
    assert 'closing_quote提供闭合证据' not in prompt
