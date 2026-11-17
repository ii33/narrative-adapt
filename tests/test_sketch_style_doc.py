"""素描风格与资产标准得有据可查，且提示词说的是同一件事。"""
from pathlib import Path

from narrative.prompt_defaults import DEFAULT_PROMPTS

ROOT = Path(__file__).resolve().parents[1]


def test_sketch_style_doc_keeps_the_shared_requirements():
    text = (ROOT / 'docs' / 'screenplay' / 'SKETCH_STYLE.md').read_text(encoding='utf-8')
    for phrase in ('事件与空间可读', '光照要在主体上产生结果', '按实际照明选择信息', '区分三种灰'):
        assert phrase in text
    assert '灰阶不决定颜色' in text or '不从灰阶猜颜色' in text
    # 五步绘制顺序：从定观看重点一路到删装饰。
    assert '明确这一镜要让观众知道什么' in text and '删掉重复轮廓' in text


def test_sketch_style_doc_references_its_images():
    doc = ROOT / 'docs' / 'screenplay' / 'SKETCH_STYLE.md'
    text = doc.read_text(encoding='utf-8')
    for name in ('fig-1-reading.png', 'fig-2-waiting.png', 'fig-3-valley.png', 'fig-4-light-fix.png'):
        assert name in text
        assert (doc.parent / 'sketch-style' / name).is_file()


def test_asset_standard_states_the_division_of_labour():
    text = (ROOT / 'docs' / 'screenplay' / 'ASSET_STANDARD.md').read_text(encoding='utf-8')
    assert '文字版分镜稿' in text and '图格版分镜稿' in text and '动态分镜' in text
    for phrase in ('角色', '场景', '物品', 'node_id', '先搜，再绑，最后才生成'):
        assert phrase in text


def test_sketch_prompt_requires_light_in_the_subject():
    text = DEFAULT_PROMPTS['sketch_style']['text']
    assert '受光面与背光面' in text and '投影' in text
    assert '光照要在主体上产生结果' in text
    assert '灰阶不决定颜色' in text
    assert '预览' in text
