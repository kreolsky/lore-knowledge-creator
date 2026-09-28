"""ComputeNode applies the config dictionary AFTER the formulas (CIR C5)."""
import pytest
from pipeline.extractor.nodes import ComputeNode, RenderNode


@pytest.mark.asyncio
async def test_compute_node_applies_dictionary_after_calculations():
    node = ComputeNode()
    shared = {
        "extracted_data": {"a": "2", "b": "3", "note": "до 6 штук, 8 миллиметров"},
        "calculations": {"total": "={{a}} + {{b}}"},
        "typography": {"штук": "шт.", "миллиметров": "мм"},
    }
    prep = await node.prep_async(shared)
    out = await node.exec_async(prep)
    await node.post_async(shared, prep, out)
    assert str(shared["extracted_data"]["total"]) in ("5", "5.0")
    assert shared["extracted_data"]["note"] == "до 6 шт., 8 мм"


@pytest.mark.asyncio
async def test_compute_node_without_dictionary_is_unchanged():
    node = ComputeNode()
    shared = {"extracted_data": {"note": "до 6 штук"}, "calculations": {}, "typography": {}}
    prep = await node.prep_async(shared)
    out = await node.exec_async(prep)
    assert out["note"] == "до 6 штук"


RANGES = {
    "uterus_length": [
        {"name": "ниже", "max": 40},
        {"name": "норма", "min": 40, "max": 60},
        {"name": "выше", "min": 60},
    ],
}


@pytest.mark.asyncio
async def test_compute_node_range_flag_flows_to_render():
    node = ComputeNode()
    shared = {
        "extracted_data": {"uterus_length": "52"},
        "calculations": {"uterus_length_flag": "=range({{uterus_length}}, uterus_length)"},
        "typography": {},
        "ranges": RANGES,
    }
    prep = await node.prep_async(shared)
    out = await node.exec_async(prep)
    await node.post_async(shared, prep, out)
    assert shared["extracted_data"]["uterus_length_flag"] == "норма"

    shared["template_string"] = "Флаг: {{uterus_length_flag}}"
    rnode = RenderNode()
    rprep = await rnode.prep_async(shared)
    rendered = await rnode.exec_async(rprep)
    assert "норма" in rendered
