import logging

import pytest
from pipeline.core.config import (
    resolve_ranges_doc,
    resolve_typography_doc,
    validate_mark_tables,
)


@pytest.mark.asyncio
async def test_resolve_typography_doc_parses_replacements():
    rows = [{"title": "typography", "content": '```yaml\nreplacements:\n  "штук": "шт."\n  "миллиметров": "мм"\n```'}]
    got = await resolve_typography_doc("cfg", rows=rows)
    assert got == {"штук": "шт.", "миллиметров": "мм"}


@pytest.mark.asyncio
async def test_resolve_typography_doc_absent_or_empty_returns_empty():
    assert await resolve_typography_doc("cfg", rows=[]) == {}
    assert await resolve_typography_doc("cfg", rows=[{"title": "typography", "content": "(empty)"}]) == {}


# ── resolve_ranges_doc ─────────────────────────────────────────────────────


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


# ── ranges parsing: bound-table shapes (attributes, is, optional name) ──────


@pytest.mark.asyncio
async def test_resolve_ranges_doc_bad_subtable_reason_names_it():
    content = (
        "```yaml\n"
        "m_echo_thickness:\n"
        "  by: postmenopause\n"
        "  \"0\": [{flag: норма, max: \"15\"}]\n"
        "```\n"
    )
    rejected: dict[str, str] = {}
    await resolve_ranges_doc(
        "cfg", rows=[{"title": "ranges", "content": content}], rejected_sink=rejected,
    )
    assert "sub-table '0'" in rejected["m_echo_thickness"]
    assert "max must be a number" in rejected["m_echo_thickness"]


# ── validate_mark_tables — Setup-time gates for tables bound to variables ────


VARS = {
    "uterus_length": "Uterus length in mm",
    "m_echo_thickness": "M-echo thickness",
    "postmenopause": "Postmenopause",
    "cervix_state": "Cervix state",
    "uterus_volume": "Calculated volume",
}
KINDS = {"cervix_state": "enum"}
ENUMS = {"cervix_state": ["деформирована", "укорочена", "норма"]}
TYPES = {
    "uterus_length": "number",
    "m_echo_thickness": "string",
    "postmenopause": "integer",
}
CALC = {"uterus_volume": "={{a}} * {{b}}"}


def _validate(tables, **overrides):
    kwargs = {
        "variables": VARS,
        "calculations": CALC,
        "kinds": KINDS,
        "enums": ENUMS,
        "types": TYPES,
    }
    kwargs.update(overrides)
    validate_mark_tables(tables, **kwargs)


def test_validate_mark_table_matching_no_variable_not_validated():
    tables = {
        "unknown_table": [
            {"flag": "норма", "min": 0},  # attrs, but no variable with this name
        ],
    }
    _validate(tables)  # must not raise — stays a plain range() table
