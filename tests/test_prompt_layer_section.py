"""五层段的形状守卫（`extraction`）：用户逐句改过这一整段，措辞不再动，只钉形状。

为什么单独立一个文件：`tests/test_extraction_granularity_prompt.py` 断言的是「哪句在不在」，
一段被重写就整片红；这里只锁**结构**——不含问号、五层各一行且不超长、「五层都是「事件」」
只说一次、场景那两句必须在场、整段不超过 800 字符。形状一破，说明有人又往这一段里加
条件句或问句了（那样它就不再是「五层是什么」而是判据清单）。

顺带把 `U-13` 一起钉住：`storyline_progress`／`big_event_progress` 两个属性名已经删掉，
提示词里任何一处再教模型写它们都是回潮。
"""
from __future__ import annotations

from narrative.prompt_defaults import DEFAULT_PROMPTS

LAYER_HEAD = "五层结构："
SECTION_END = "六条原则"
LAYER_LINES = ("部／卷（主要故事 T）", "阶段（大事件 B）", "现场（场景 S）", "事件（事件 E）", "动作（动作 A）")
# 概念版每行以「- 概念名（层名）：」开头，例如「- 部／卷（主要故事 T）：…」：
# 概念名在前、代号在后，模型读到的第一句就是「这一层是什么」，不是「T 是什么」。
SECTION_LIMIT = 800
LINE_LIMIT = 120


def layer_section() -> str:
    """从「五层结构：」到「六条原则」之前：这一段就是契约三整段替换的正文。"""
    text = DEFAULT_PROMPTS["extraction"]["text"]
    start = text.index(LAYER_HEAD)
    end = text.index(SECTION_END, start)
    return text[start:end].strip("\n")


def test_layer_section_has_no_question_marks():
    """五层段是定义，不是反问清单：出现问号就说明又写出了一份要模型自答的判据。"""
    section = layer_section()
    assert "？" not in section
    assert "?" not in section


def test_each_layer_occupies_exactly_one_short_line():
    """五层各一行、按由粗到细排、每行不超过 120 字。"""
    lines = [line.strip() for line in layer_section().splitlines() if line.strip()]
    bullets = [line for line in lines if line.startswith("- ")]
    assert len(bullets) == len(LAYER_LINES), f"五层段应有 {len(LAYER_LINES)} 条要点，实际 {len(bullets)} 条"
    for label, bullet in zip(LAYER_LINES, bullets, strict=True):
        assert bullet.startswith(f"- {label}："), f"第 {label} 行没按「- {label}：」开头：{bullet[:24]}"
        assert len(bullet) <= LINE_LIMIT, f"「{label}」这一行 {len(bullet)} 字，超过 {LINE_LIMIT} 字"


def test_the_layer_statement_is_said_once():
    """「五层都是「事件」」只出现一次：说两遍就是两套说法，早晚漂移。"""
    extraction = DEFAULT_PROMPTS["extraction"]["text"]
    assert extraction.count("五层都是「事件」") == 1
    assert layer_section().count("五层都是「事件」") == 1
    # 别的提示词里也不许出现同一句话（同一句出现在两处就是第二个真源）。
    others = [key for key, entry in DEFAULT_PROMPTS.items() if "五层都是「事件」" in entry["text"]]
    assert others == ["extraction"], f"「五层都是「事件」」还出现在：{others}"


def test_scene_sentences_that_must_stay():
    """场景两条：只装一个事件也算一个现场；地点记在事件上、不记在场景上（`U-09` 的挂靠口径）。"""
    section = layer_section()
    assert "只装一个事件也算" in section
    assert "地点记在事件上，不记在场景上" in section


def test_layer_section_stays_short():
    """整段不超过 800 字符：这一段是「五层是什么」，不是细则集合。"""
    section = layer_section()
    assert len(section) <= SECTION_LIMIT, f"五层段 {len(section)} 字符，超过 {SECTION_LIMIT}"


def test_container_paragraph_is_the_two_sentence_version():
    """容器段压成两句：不填定位依据 + 程序派生 + 模型只判边界与直属关系。"""
    section = layer_section()
    assert "容器（T／B／S）不填定位依据" in section
    assert "模型只判边界、名称与直属关系" in section


def test_progress_properties_are_gone_from_every_prompt():
    """`U-13`：主要故事／大事件的「进展」就是当前生效的名字，不再有两个进展属性名。"""
    stale = [
        key for key, entry in DEFAULT_PROMPTS.items()
        if "storyline_progress" in entry["text"] or "big_event_progress" in entry["text"]
    ]
    assert stale == [], f"这些提示词还在教模型写进展属性：{stale}"
