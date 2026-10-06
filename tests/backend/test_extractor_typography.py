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
    # `by` is KEPT on the parsed conditional table — it is
    # functional for tables bound to a variable (validate_mark_tables); range()
    # keeps ignoring it.
    assert got["m_echo_thickness"] == {
        "by": "postmenopause",
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
async def test_resolve_ranges_doc_by_key_kept_and_int_subtable_keys_stringified():
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
        "by": "postmenopause",
        "0": [{"name": "норма", "max": 15}, {"name": "выше", "min": 15}],
        "1": [{"name": "норма", "max": 5}],
    }
    assert got["m_echo_thickness"]["by"] == "postmenopause"


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
async def test_resolve_ranges_doc_keeps_attributes_is_and_optional_name():
    content = (
        "```yaml\n"
        "uterus_length:\n"
        '  - {flag: ниже нормы, color: 8ab4ff, view: "{{value}}", max: 40}\n'
        '  - {flag: норма, color: 8ab440, view: "{{value}}", min: 40, max: 60}\n'
        '  - {flag: выше нормы, color: ec883c, view: "**{{value}}**", min: 60}\n'
        '  - {view: "{{value}}"}\n'
        "cervix_state:\n"
        '  - {is: [деформирована, укорочена], flag: патология, view: "**{{value}}**"}\n'
        '  - {flag: норма, view: "{{value}}"}\n'
        "```\n"
    )
    got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": content}])
    assert got["uterus_length"][0] == {
        "flag": "ниже нормы", "color": "8ab4ff", "view": "{{value}}", "max": 40,
    }
    assert got["uterus_length"][3] == {"view": "{{value}}"}  # name optional
    assert got["cervix_state"][0]["is"] == ["деформирована", "укорочена"]
    assert got["cervix_state"][0]["flag"] == "патология"


@pytest.mark.asyncio
async def test_resolve_ranges_doc_drops_table_with_struct_attr_value(caplog):
    # An attribute value that is a list/dict is a wrong shape — the whole field drops.
    content = (
        "```yaml\n"
        "uterus_length:\n"
        "  - {flag: [a, b], max: 40}\n"
        "```\n"
    )
    with caplog.at_level(logging.WARNING):
        got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": content}])
    assert got == {}
    assert "uterus_length" in caplog.text


@pytest.mark.asyncio
async def test_resolve_ranges_doc_scalar_is_rejected_with_reason():
    # `is: x` instead of `is: [x]` is the likeliest author slip — the drop must
    # carry a reason that tells the author how to write it.
    content = (
        "```yaml\n"
        "cervix_state:\n"
        "  - {is: укорочена, flag: патология}\n"
        "  - {flag: норма}\n"
        "```\n"
    )
    rejected: dict[str, str] = {}
    got = await resolve_ranges_doc(
        "cfg", rows=[{"title": "ranges", "content": content}], rejected_sink=rejected,
    )
    assert got == {}
    assert "is: [укорочена]" in rejected["cervix_state"]


@pytest.mark.asyncio
async def test_resolve_ranges_doc_yaml_boolean_attribute_rejected():
    content = (
        "```yaml\n"
        "uterus_length:\n"
        "  - {flag: yes, max: 40}\n"
        "  - {flag: \"no\"}\n"
        "```\n"
    )
    rejected: dict[str, str] = {}
    got = await resolve_ranges_doc(
        "cfg", rows=[{"title": "ranges", "content": content}], rejected_sink=rejected,
    )
    assert got == {}
    assert "row 1" in rejected["uterus_length"]
    assert "boolean" in rejected["uterus_length"]


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


@pytest.mark.asyncio
async def test_resolve_ranges_doc_lone_conditional_table_is_not_unwrapped():
    # A ranges doc holding ONE conditional table: the generic single-key unwrap
    # would turn `by` and the sub-table keys into tables of their own.
    content = (
        "```yaml\n"
        "m_echo_thickness:\n"
        "  by: postmenopause\n"
        "  \"0\": [{flag: норма, max: 15}]\n"
        "  \"1\": [{flag: норма, max: 5}]\n"
        "```\n"
    )
    got = await resolve_ranges_doc("cfg", rows=[{"title": "ranges", "content": content}])
    assert list(got) == ["m_echo_thickness"]
    assert got["m_echo_thickness"]["by"] == "postmenopause"


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

BOUND_TABLE = {
    "uterus_length": [
        {"flag": "ниже нормы", "color": "8ab4ff", "view": "{{value}}", "max": 40},
        {"flag": "норма", "color": "8ab440", "view": "{{value}}", "min": 40, "max": 60},
        {"flag": "выше нормы", "color": "ec883c", "view": "**{{value}}**", "min": 60},
        {"view": "{{value}}"},
    ],
}


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


def test_validate_mark_tables_accepts_target_shape():
    tables = {
        **BOUND_TABLE,
        "cervix_state": [
            {"is": ["деформирована", "укорочена"], "flag": "патология", "view": "**{{value}}**"},
            {"flag": "норма", "view": "{{value}}"},
        ],
        "m_echo_thickness": {
            "by": "postmenopause",
            "0": [{"flag": "норма", "max": 15}, {"flag": "выше", "min": 15}],
            "1": [{"flag": "норма", "max": 5}, {"flag": "критично", "min": 5}],
        },
        # a calculate: output is a legal binding target (flag-only: no catch-all due)
        "uterus_volume": [{"flag": "большой", "min": 100}, {"flag": "обычный"}],
    }
    _validate(tables)  # must not raise


def test_validate_mark_table_view_without_catch_all_raises():
    tables = {
        "uterus_length": [
            {"flag": "норма", "view": "{{value}}", "min": 40, "max": 60},
        ],
    }
    with pytest.raises(ValueError, match="catch-all"):
        _validate(tables)


def test_validate_mark_table_flag_without_catch_all_is_legal():
    # Only a {{value}} attribute forces the catch-all — a flag-only table may
    # legitimately cover just its intervals.
    tables = {
        "uterus_length": [
            {"flag": "норма", "min": 40, "max": 60},
        ],
    }
    _validate(tables)  # must not raise


def test_validate_mark_table_foreign_placeholder_raises():
    tables = {
        "uterus_length": [
            {"view": "{{uterus_length}}", "min": 0},
            {"view": ""},
        ],
    }
    with pytest.raises(ValueError, match="may only contain"):
        _validate(tables)


def test_validate_mark_table_non_identifier_attr_key_raises():
    tables = {
        "uterus_length": [
            {"флаг 1": "норма", "min": 0},
        ],
    }
    with pytest.raises(ValueError, match="флаг 1"):
        _validate(tables)


def test_validate_mark_table_numeric_on_enum_variable_raises():
    tables = {
        "cervix_state": [
            {"flag": "патология", "min": 0},
        ],
    }
    with pytest.raises(ValueError, match="cervix_state"):
        _validate(tables)


def test_validate_mark_table_numeric_on_boolean_variable_raises():
    tables = {
        "pregnant": [
            {"flag": "патология", "min": 0},
        ],
    }
    with pytest.raises(ValueError, match="pregnant"):
        _validate(
            tables,
            variables={**VARS, "pregnant": "Pregnancy"},
            types={**TYPES, "pregnant": "boolean"},
        )


def test_validate_mark_table_numeric_on_string_variable_ok():
    # Live configs run range() over string-typed measurements — string stays legal.
    tables = {
        "m_echo_thickness": [
            {"flag": "норма", "max": 15},
            {"flag": "выше", "min": 15},
        ],
    }
    _validate(tables)  # must not raise


def test_validate_mark_table_categorical_is_value_not_in_options_raises():
    tables = {
        "cervix_state": [
            {"is": ["деформирована", "опущена"], "flag": "патология"},  # "опущена" is a typo
            {"flag": "норма"},
        ],
    }
    with pytest.raises(ValueError, match="опущена"):
        _validate(tables)


def test_validate_mark_table_categorical_on_prefix_raises():
    tables = {
        "canal": [
            {"is": ["расширен"], "flag": "патология"},
            {"flag": "норма"},
        ],
    }
    with pytest.raises(ValueError, match="canal"):
        _validate(
            tables,
            variables={**VARS, "canal": "Canal"},
            kinds={**KINDS, "canal": "prefix"},
            enums={**ENUMS, "canal": ["расширен", "не расширен"]},
        )


def test_validate_mark_table_categorical_on_multiselect_raises():
    tables = {
        "access": [
            {"is": ["x"], "flag": "патология"},
            {"flag": "норма"},
        ],
    }
    with pytest.raises(ValueError, match="access"):
        _validate(
            tables,
            variables={**VARS, "access": "Access"},
            kinds={**KINDS, "access": "multiselect"},
            enums={**ENUMS, "access": ["x", "y"]},
        )


def test_validate_mark_table_categorical_on_number_raises():
    tables = {
        "uterus_length": [
            {"is": ["deformed"], "flag": "патология"},
            {"flag": "норма"},
        ],
    }
    with pytest.raises(ValueError, match="uterus_length"):
        _validate(tables)


def test_validate_mark_table_categorical_on_calculate_output_raises():
    tables = {
        "uterus_volume": [
            {"is": ["big"], "flag": "патология"},
            {"flag": "норма"},
        ],
    }
    with pytest.raises(ValueError, match="uterus_volume"):
        _validate(tables)


def test_validate_mark_table_mixed_is_and_bounds_raises():
    tables = {
        "cervix_state": [
            {"is": ["деформирована"], "flag": "патология"},
            {"flag": "норма", "min": 0},
        ],
    }
    with pytest.raises(ValueError, match="cervix_state"):
        _validate(tables)


def test_validate_mark_table_conditional_without_by_raises():
    tables = {
        "m_echo_thickness": {
            "0": [{"flag": "норма", "max": 15}],
            "1": [{"flag": "критично", "min": 5}],
        },
    }
    with pytest.raises(ValueError, match="by"):
        _validate(tables)


def test_validate_mark_table_conditional_by_undeclared_variable_raises():
    tables = {
        "m_echo_thickness": {
            "by": "menopause",  # typo: the variable is postmenopause
            "0": [{"flag": "норма", "max": 15}],
        },
    }
    with pytest.raises(ValueError, match="menopause"):
        _validate(tables)


def test_validate_mark_table_conditional_by_naming_calculate_output_ok():
    tables = {
        "m_echo_thickness": {
            "by": "uterus_volume",  # a calculate: output is a declared variable
            "0": [{"flag": "норма", "max": 15}],
        },
    }
    _validate(tables)  # must not raise


def test_validate_mark_table_conditional_catch_all_per_subtable():
    tables = {
        "m_echo_thickness": {
            "by": "postmenopause",
            "0": [{"flag": "норма", "max": 15}, {"view": "{{value}}"}],
            "1": [{"flag": "критично", "min": 5}],  # no {{value}} → no catch-all needed
        },
    }
    _validate(tables)  # must not raise
    broken = {
        "m_echo_thickness": {
            "by": "postmenopause",
            "0": [{"flag": "норма", "max": 15}],
            "1": [{"view": "{{value}}", "min": 0}],  # {{value}} and no catch-all in "1"
        },
    }
    with pytest.raises(ValueError, match="catch-all"):
        _validate(broken)


def test_validate_mark_table_name_only_table_not_validated():
    # Rows of bare {name, min, max} intervals: a plain range() table, never bound —
    # even though its name matches a variable, and even on an enum variable.
    tables = {
        "uterus_length": [
            {"name": "норма", "min": 40, "max": 60},
        ],
    }
    _validate(tables)  # must not raise


def test_validate_mark_table_matching_no_variable_not_validated():
    tables = {
        "unknown_table": [
            {"flag": "норма", "min": 0},  # attrs, but no variable with this name
        ],
    }
    _validate(tables)  # must not raise — stays a plain range() table


def test_validate_mark_table_generated_collision_raises():
    tables = {
        "uterus_length": [
            {"uterus_length_flag": "x", "min": 0},  # <var>_<key> equals... nothing yet
        ],
    }
    _validate(tables)  # "uterus_length_uterus_length_flag" collides with nothing — ok
    colliding = {
        "uterus_length": [
            {"volume": "x", "min": 0},
        ],
    }
    with pytest.raises(ValueError, match="collision|collides"):
        _validate(colliding, variables={**VARS, "uterus_length_volume": "Colliding"})


def test_validate_mark_table_generated_collision_with_calculate_output_raises():
    tables = {
        "uterus_length": [
            {"flag": "норма", "min": 0},
        ],
    }
    with pytest.raises(ValueError, match="uterus_length_flag"):
        _validate(tables, calculations={**CALC, "uterus_length_flag": "={{x}}"})


def test_validate_mark_table_name_attribute_generates_nothing():
    # `name` is range()'s return value only — no <var>_name variable, so a table
    # whose only extra key is name is NOT bound.
    tables = {
        "uterus_length": [
            {"name": "норма", "min": 40, "max": 60, "name_note": "x"},
        ],
    }
    _validate(tables)  # name_note makes it bound; nothing collides — must not raise


def test_validate_mark_tables_rejected_table_named_after_variable_raises():
    # A malformed table named after a declared variable must FAIL the run, not
    # drop silently and leave {{<var>_<attr>}} empty in the report.
    with pytest.raises(ValueError, match=r"cervix_state.*malformed.*is: \[укорочена\]"):
        _validate({}, rejected={"cervix_state": "row 1: write is: [укорочена]"})


def test_validate_mark_tables_rejected_table_named_after_calculate_output_raises():
    with pytest.raises(ValueError, match="uterus_volume"):
        _validate({}, rejected={"uterus_volume": "expected a non-empty list of rows"})


def test_validate_mark_tables_rejected_table_matching_no_variable_stays_a_warning():
    _validate({}, rejected={"bogus": "expected a list of rows"})  # must not raise
