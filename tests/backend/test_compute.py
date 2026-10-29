"""Unit tests for the expression evaluator and calculation processor."""
import logging

import pytest
from pipeline.extractor.compute import (
    apply_mark_tables,
    evaluate_expression,
    process_calculations,
)

# ── evaluate_expression ────────────────────────────────────────────────────


def test_evaluate_negative_literal():
    assert evaluate_expression("=-5") == -5


def test_evaluate_complex_expression():
    result = evaluate_expression(
        "=round(({{var1}}/2) * ({{var2}}/2) * 3.14 * (4/3), 2)",
        {"var1": 10, "var2": 20},
    )
    assert result == 209.33


# ── process_calculations ───────────────────────────────────────────────────


def test_process_calculations_empty_returns_extracted():
    extracted = {"x": 10}
    assert process_calculations(extracted, {}) == extracted


def test_process_calculations_topological_order():
    extracted = {}
    calculations = {
        "c": "={{a}} + {{b}}",
        "a": "=1",
        "b": "=2",
    }
    result = process_calculations(extracted, calculations)
    assert result == {"a": 1, "b": 2, "c": 3}


# ── range(value, table[, key]) — reference intervals ────────────────────────

FLAT_RANGES = {
    "uterus_length": [
        {"name": "ниже", "max": 40},
        {"name": "норма", "min": 40, "max": 60},
        {"name": "выше", "min": 60},
    ],
}

CONDITIONAL_RANGES = {
    "m_echo_thickness": {
        "0": [{"name": "норма", "max": 15}, {"name": "выше", "min": 15}],
        "1": [{"name": "норма", "max": 5}, {"name": "критично", "min": 5}],
    },
}


def test_range_none_value_returns_empty():
    assert evaluate_expression("=range({{v}}, uterus_length)", {"v": None}, ranges=FLAT_RANGES) == ""


def test_range_conditional_key_not_found_returns_empty_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        got = evaluate_expression(
            "=range({{v}}, m_echo_thickness, {{k}})",
            {"v": 10, "k": "фолликулярная"},
            ranges=CONDITIONAL_RANGES,
        )
    assert got == ""
    assert "m_echo_thickness" in caplog.text


def test_range_conditional_key_true_hits_subtable_one():
    got = evaluate_expression(
        "=range({{v}}, m_echo_thickness, {{k}})",
        {"v": 10, "k": True},
        ranges=CONDITIONAL_RANGES,
    )
    assert got == "критично"


def test_range_result_fed_back_into_arithmetic_raises():
    flag = evaluate_expression("=range({{v}}, uterus_length)", {"v": 50}, ranges=FLAT_RANGES)
    assert flag == "норма"
    with pytest.raises(ValueError, match=r"(?i)cannot be coerced"):
        evaluate_expression("={{flag}} + 1", {"flag": flag})


# ── apply_mark_tables — ranges tables BOUND to variables ────────────────────

BOUND_RANGES = {
    "uterus_length": [
        {"flag": "ниже нормы", "color": "8ab4ff", "view": "{{value}}", "max": 40},
        {"flag": "норма", "color": "8ab440", "view": "{{value}}", "min": 40, "max": 60},
        {"flag": "выше нормы", "color": "ec883c", "view": "**{{value}}**", "min": 60},
        {"view": "{{value}}"},  # catch-all
    ],
}

BOUND_CONDITIONAL = {
    "m_echo_thickness": {
        "by": "postmenopause",
        "0": [{"flag": "норма", "max": 15}, {"flag": "выше", "min": 15}],
        "1": [{"flag": "норма", "max": 5}, {"flag": "критично", "min": 5}],
    },
}

BOUND_CATEGORICAL = {
    "cervix_state": [
        {"is": ["деформирована", "укорочена"], "flag": "патология", "view": "**{{value}}**"},
        {"flag": "норма", "view": "{{value}}"},
    ],
}


def test_mark_table_empty_value_all_attributes_empty():
    out = apply_mark_tables({"uterus_length": ""}, BOUND_RANGES)
    assert out["uterus_length_flag"] == ""
    assert out["uterus_length_view"] == ""
    assert out["uterus_length_color"] == ""
    out = apply_mark_tables({"uterus_length": None}, BOUND_RANGES)
    assert out["uterus_length_view"] == ""


def test_mark_table_rows_may_omit_attributes():
    tables = {
        "v": [
            {"flag": "норма", "color": "red", "min": 0, "max": 10},
            {"flag": "выше", "min": 10},
        ],
    }
    out = apply_mark_tables({"v": 20}, tables)
    assert out["v_flag"] == "выше"
    assert out["v_color"] == ""  # the matched row omits it


def test_mark_table_conditional_empty_by_value_all_empty():
    out = apply_mark_tables(
        {"m_echo_thickness": 10, "postmenopause": ""}, BOUND_CONDITIONAL
    )
    assert out["m_echo_thickness_flag"] == ""


def test_mark_table_conditional_unknown_key_all_empty_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        out = apply_mark_tables(
            {"m_echo_thickness": 10, "postmenopause": "фолликулярная"}, BOUND_CONDITIONAL
        )
    assert out["m_echo_thickness_flag"] == ""
    assert "m_echo_thickness" in caplog.text


def test_mark_table_categorical_enum_catch_all():
    out = apply_mark_tables({"cervix_state": "норма"}, BOUND_CATEGORICAL)
    assert out["cervix_state_flag"] == "норма"
    assert out["cervix_state_view"] == "норма"


def test_mark_table_name_matching_no_variable_generates_nothing():
    tables = {
        "no_such_var": [
            {"flag": "норма", "min": 0},
        ],
    }
    out = apply_mark_tables({"other": 1}, tables)
    assert out == {"other": 1}


def test_mark_table_conditional_is_rows_inside_subtables():
    tables = {
        "cervix_state": {
            "by": "postmenopause",
            "0": [{"is": ["деформирована"], "flag": "патология"}, {"flag": "норма"}],
            "1": [{"flag": "всё норма"}],
        },
    }
    out = apply_mark_tables(
        {"cervix_state": "деформирована", "postmenopause": 0}, tables
    )
    assert out["cervix_state_flag"] == "патология"
    out = apply_mark_tables(
        {"cervix_state": "деформирована", "postmenopause": 1}, tables
    )
    assert out["cervix_state_flag"] == "всё норма"
