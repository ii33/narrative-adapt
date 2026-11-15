"""最上位原则必须写在文档里：事件是时间点，事件链是时间轴。

这条原则从项目早期就在口头强调，但很长一段时间只以「事件是时间轴上的一个点」一句话
散在 `docs/MODEL.md` 的要点里，没有写成独立原则，也没有推出「其它对象的原文位置来自
它挂靠的事件」这条结论——于是实现各写各的（实体要不要自带引文、阅读位置切点查不查得到
人物，都靠临场理解）。这里把它钉住：文档缺这句就红。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "docs" / "MODEL.md"
SPEC = ROOT / "docs" / "extraction" / "SPEC.md"


def test_model_states_the_event_timeline_principle():
    text = MODEL.read_text(encoding="utf-8")
    assert "事件是时间点" in text and "事件链是时间轴" in text
    # 原则要推出可执行的结论，而不只是一句口号。
    assert "原文位置只在事件上" in text


def test_spec_draws_the_same_conclusion_for_entities():
    text = SPEC.read_text(encoding="utf-8")
    assert "事件是时间点" in text and "事件链是时间轴" in text
    assert "最早挂靠的事件" in text or "挂靠的事件" in text


def test_numbering_is_the_order_within_one_type():
    """同一类型内编号就是顺序（发号按读者遇到它的顺序）；跨类型不可比。"""
    model = MODEL.read_text(encoding="utf-8")
    spec = SPEC.read_text(encoding="utf-8")
    for text in (model, spec):
        assert "同一类型内" in text
        assert "跨类型" in text
    # 编号机制本身要有可引用出处。
    assert "order_ids.py" in model


def test_only_events_need_their_own_pointer():
    """只有事件需要自带原文位置；这条是上面原则的直接推论，写在规范的裁定里。"""
    text = SPEC.read_text(encoding="utf-8")
    assert "只有事件" in text
    assert "anchor" in text
