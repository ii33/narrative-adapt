"""Both extraction modes teach explicit location state without guessing facts."""
from narrative.extraction_draft import RESULT_SCHEMA
from narrative.prompt_defaults import DEFAULT_PROMPTS


def test_shared_extraction_guide_requires_each_events_own_world_location():
    text = DEFAULT_PROMPTS['extraction']['text']
    for expected in ['每个事件都必须', 'world', '地点', 'either', 'location', '“无”', '“未知”',
                     'at_event必须指向该事件自身', '不能省略、臆造地点']:
        assert expected in text
    assert '场景地点、participants或人物认知' in text


def test_draft_value_contract_carries_location_reference_and_sentinel_choices():
    fields = RESULT_SCHEMA['properties']['properties']['items']['properties']
    assert '每个event必须有自身world地点' in fields['value']['description']
    assert 'either' in fields['value_type']['description']
    assert 'op=unknown' in fields['value']['description']
