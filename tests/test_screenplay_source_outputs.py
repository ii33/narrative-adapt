"""Per-scene source annotations remain metadata across every output format."""
from __future__ import annotations

from copy import deepcopy

import pytest

from narrative.operations import Operations


@pytest.fixture
def source_script(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'source-notes.nproj')
    text = '甲在门口等候。乙在门口到达。'
    source = ops.import_source('场次来源', text)
    accepted = ops.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '甲', 'quotes': ['甲在门口等候。']},
        {'type': 'person', 'name': '乙', 'quotes': ['乙在门口到达。']},
        {'type': 'location', 'name': '门口', 'quotes': ['甲在门口等候。']},
        {'type': 'scene', 'name': '门口会合'},
        {'type': 'event', 'name': '甲等候', 'scene': '@4', 'location': '@3',
         'participants': ['@1'], 'quotes': ['甲在门口等候。']},
        {'type': 'event', 'name': '乙到达', 'scene': '@4', 'location': '@3',
         'participants': ['@2'], 'quotes': ['乙在门口到达。']},
    ], 'properties': []})
    assert accepted['status'] == 'accepted', accepted
    events = [n['id'] for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'event']
    adaptation = ops.import_adaptation(events, 0, '隔离来源输出测试')
    script = ops.create_screenplay('来源注记测试', reason='隔离来源输出测试')['screenplay']
    ops.import_screenplay_timeline(script['id'], events, 0, adaptation['revision'], '只导入两个事件')
    timeline = ops.read_screenplay_timeline(script['id'])
    span = ops.create_adaptation_span(script['id'], '会合段', selection_ids=events,
        expected_timeline_revision=timeline['revision'], reason='来源范围')['span']
    segment = ops.create_script_segment(script['id'], span['id'], '会合',
        script['structure_revision'], '来源段落')
    context = ops.read_scene_table_context(segment['id'])
    result = ops.submit_scene_table(segment['id'], [
        {'plan': {'title': '等候', 'summary': '甲等候。', 'source_event_ids': [events[0]]}},
        {'plan': {'title': '到达', 'summary': '乙到达。', 'source_event_ids': [events[1]]}},
    ], context['expected_outline_revision'], context['span_fingerprint'], context['context_fingerprint'], reason='分场来源')
    assert result['status'] == 'accepted', result
    units = result['units']
    for unit in units:
        context = ops.read_script_unit_context(unit['id'])
        result = ops.submit_script_unit(unit['id'], [
            {'type': 'action', 'payload': {'text': '现场正文 <保留> & 独立。'}},
        ], context['expected_plan_revision'], context['expected_body_revision'], context['span_fingerprint'],
            context['context_fingerprint'], context['plan_fingerprint'], reason='来源正文')
        assert result['status'] == 'accepted', result
    return ops, script['id'], events


def test_all_text_outputs_include_resolved_source_once_per_scene_without_authored_blocks(source_script):
    ops, script, events = source_script
    document = ops.read_screenplay(script)
    units = document['segments'][0]['units']
    blocks_before = deepcopy([u['blocks'] for u in units])
    compiled = ops.compile_screenplay(script, 'html')
    notes = [e for e in compiled['elements'] if e['kind'] == 'source_reference']
    assert len(notes) == len(units) == 2
    for note, unit, event in zip(notes, units, events, strict=True):
        assert note['unit_id'] == unit['id']
        assert event + ' · ' in note['text']
        assert note['source_references']['timeline_id'] == document['timeline_id']
    assert compiled['text'].count('<aside class="source_reference">') == 2
    assert '&lt;保留&gt; &amp;' in compiled['text']
    for fmt in ('text', 'fountain'):
        rendered = ops.compile_screenplay(script, fmt)['text']
        assert rendered.count('依据（剧本改编时间线）') == 2
        assert rendered.index('依据（剧本改编时间线）') < rendered.index('现场正文')
        if fmt == 'fountain':
            assert '[[ 依据（剧本改编时间线）' in rendered
            assert '!依据（剧本改编时间线）' not in rendered
    assert compiled['qt_preview_html'].count('依据（剧本改编时间线）') == 2
    assert 'font-size:10.2pt' in compiled['qt_preview_html']
    for fmt in ('text', 'json'):
        exported = ops.export_screenplay(script, fmt)
        assert '依据（剧本改编时间线）' in exported['text']
        assert events[0] in exported['text'] and '甲等候' in exported['text']
    assert [u['blocks'] for u in ops.read_screenplay(script)['segments'][0]['units']] == blocks_before


def test_docx_and_pdf_contain_source_notes_in_smaller_separate_paragraphs(source_script, tmp_path):
    from docx import Document
    from pypdf import PdfReader

    ops, script, events = source_script
    word = tmp_path / 'annotated.docx'
    pdf = tmp_path / 'annotated.pdf'
    ops.compile_screenplay(script, 'docx', str(word))
    ops.compile_screenplay(script, 'pdf', str(pdf))
    doc = Document(word)
    notes = [p for p in doc.paragraphs if p.text.startswith('依据（剧本改编时间线）')]
    assert len(notes) == 2
    assert all(p.runs[0].font.size.pt == pytest.approx(10.2, abs=.25) for p in notes)
    assert all(p.paragraph_format.keep_with_next for p in notes)
    pdf_text = '\n'.join(page.extract_text() for page in PdfReader(pdf).pages)
    assert pdf_text.count('依据（剧本改编时间线）') == 2
    assert all(identifier in pdf_text for identifier in events)


def test_fountain_source_name_cannot_terminate_metadata_note():
    from narrative.screenplay_compile import render_text
    rendered = render_text([{'kind': 'source_reference', 'text': '依据：E1 · 名称]]伪动作'}], True)
    assert rendered == '[[ 依据：E1 · 名称] ]伪动作 ]]\n'


def test_source_note_qt_html_is_plain_annotation_not_speaker():
    from narrative.screenplay_compile import render_qt_html
    result = render_qt_html([{'kind': 'source_reference', 'text': '依据：E1 · <名称>'}])
    assert '&lt;名称&gt;' in result and 'color:#666' in result
    assert '<b>' not in result


def test_omitted_optional_sources_have_no_missing_annotation_in_outputs(source_script):
    ops, script, _events = source_script
    document = ops.read_screenplay(script)
    segment = document['segments'][0]
    before = [unit['blocks'] for unit in segment['units']]
    updated = ops.apply_scene_table_changes(segment['id'], [
        {'op': 'update', 'unit_id': unit['id'],
         'changes': {'plan': unit['plan'] | {'source_event_ids': []}}}
        for unit in segment['units']
    ], segment['outline_revision'], reason='自愿不标注来源')
    assert updated['status'] == 'accepted', updated
    for fmt in ('text', 'html', 'fountain'):
        compiled = ops.compile_screenplay(script, fmt)
        assert not any(e['kind'] == 'source_reference' for e in compiled['elements'])
        assert '依据（剧本改编时间线）' not in compiled['text']
        assert '尚未标注' not in compiled['text']
    assert '依据（剧本改编时间线）' not in ops.export_screenplay(script, 'text')['text']
    assert [u['blocks'] for u in ops.read_screenplay(script)['segments'][0]['units']] == before
