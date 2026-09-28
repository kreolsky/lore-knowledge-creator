import logging

import pytest
from pipeline.core.config import resolve_ranges_doc, resolve_typography_doc


@pytest.mark.asyncio
async def test_resolve_typography_doc_parses_replacements():
    rows = [{"title": "typography", "content": '```yaml\nreplacements:\n  "штук": "шт."\n  "миллиметров": "мм"\n```'}]
    got = await resolve_typography_doc("cfg", rows=rows)
    assert got == {"штук": "шт.", "миллиметров": "мм"}


@pytest.mark.asyncio
async def test_resolve_typography_doc_absent_or_empty_returns_empty():
    assert await resolve_typography_doc("cfg", rows=[]) == {}
    assert await resolve_typography_doc("cfg", rows=[{"title": "typography", "content": "(empty)"}]) == {}


def test_apply_dictionary_writes_the_replacement_verbatim():
    """The replacement is printed EXACTLY as configured, whatever the match's case.

    Operator decision 2026-07-27: matching is case-insensitive, the output is not
    re-cased — the table is the single source of the printed form. Consequence to know:
    a sentence-initial 'За счет' comes back as the table's lowercase 'за счёт'; a form
    that must stay capitalized is spelled that way in the table's own key.
    """
    from pipeline.extractor.utils import apply_dictionary
    out = apply_dictionary(
        {"a": "За счет включения", "b": "изменено за счет включения"},
        {"за счет": "за счёт"},
    )
    assert out["a"] == "за счёт включения"
    assert out["b"] == "изменено за счёт включения"


# ── resolve_ranges_doc ─────────────────────────────────────────────────────


RANGES_DOC = (
    "```yaml\n"
    "uterus_length:\n"
    "  - {name: ниже, max: 40}\n"
    "  - {name: норма, min: 40, max: 60}\n"
    "  - {name: выше, min: 60}\n"
    "m_echo_thickness:\n"
    '  by: postmenopause\n'
    '  "0":\n'
    "    - {name: норма, max: 15}\n"
    "    - {name: выше, min: 15}\n"
    '  "1":\n'
    "    - {name: норма, max: 5}\n"
    "    - {name: критично, min: 5}\n"
    "```\n"
)


@pytest.mark.asyncio
async def test_resolve_ranges_doc_parses_flat_and_conditional_tables():
    got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": RANGES_DOC}])
    assert got["uterus_length"] == [
        {"name": "ниже", "max": 40},
        {"name": "норма", "min": 40, "max": 60},
        {"name": "выше", "min": 60},
    ]
    assert got["m_echo_thickness"] == {
        "0": [{"name": "норма", "max": 15}, {"name": "выше", "min": 15}],
        "1": [{"name": "норма", "max": 5}, {"name": "критично", "min": 5}],
    }


@pytest.mark.asyncio
async def test_resolve_ranges_doc_absent_or_empty_returns_empty():
    assert await resolve_ranges_doc("cfg", rows=[]) == {}
    assert await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": ""}]) == {}


@pytest.mark.asyncio
async def test_resolve_ranges_doc_unparseable_returns_empty_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        got = await resolve_ranges_doc(
            "cfg", rows=[{"title": "ranges", "content": "```yaml\nkey: [unclosed\n```"}]
        )
    assert got == {}
    assert "unparseable" in caplog.text.lower()


@pytest.mark.asyncio
async def test_resolve_ranges_doc_wrong_shaped_field_dropped_with_warning(caplog):
    content = (
        "```yaml\n"
        "uterus_length:\n"
        "  - {name: норма, min: 40, max: 60}\n"
        "bogus: 42\n"
        "```\n"
    )
    with caplog.at_level(logging.WARNING):
        got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": content}])
    assert list(got) == ["uterus_length"]
    assert "bogus" in caplog.text


@pytest.mark.asyncio
async def test_resolve_ranges_doc_by_key_stripped_and_int_subtable_keys_stringified():
    content = (
        "```yaml\n"
        "uterus_length:\n"
        "  - {name: норма, min: 40, max: 60}\n"
        "m_echo_thickness:\n"
        "  by: postmenopause\n"
        "  0:\n"
        "    - {name: норма, max: 15}\n"
        "    - {name: выше, min: 15}\n"
        "  1:\n"
        "    - {name: норма, max: 5}\n"
        "```\n"
    )
    got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": content}])
    assert got["m_echo_thickness"] == {
        "0": [{"name": "норма", "max": 15}, {"name": "выше", "min": 15}],
        "1": [{"name": "норма", "max": 5}],
    }
    assert "by" not in got["m_echo_thickness"]


@pytest.mark.asyncio
async def test_resolve_ranges_doc_non_identifier_table_dropped_with_warning(caplog):
    content = (
        "```yaml\n"
        "uterus_length:\n"
        "  - {name: норма, min: 40, max: 60}\n"
        "m-echo:\n"
        "  - {name: норма, max: 15}\n"
        "```\n"
    )
    with caplog.at_level(logging.WARNING):
        got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": content}])
    assert list(got) == ["uterus_length"]
    assert "m-echo" in caplog.text
