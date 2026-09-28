"""Unit tests for the expression evaluator and calculation processor."""
import logging

import pytest
from pipeline.extractor.compute import (
    ExpressionEvaluator,
    evaluate_expression,
    process_calculations,
)

# ── evaluate_expression ────────────────────────────────────────────────────


def test_evaluate_literal_int():
    assert evaluate_expression("=42") == 42


def test_evaluate_literal_float():
    assert evaluate_expression("=3.14") == 3.14


def test_evaluate_negative_literal():
    assert evaluate_expression("=-5") == -5


def test_evaluate_addition():
    assert evaluate_expression("=2 + 3") == 5


def test_evaluate_subtraction():
    assert evaluate_expression("=10 - 3") == 7


def test_evaluate_multiplication():
    assert evaluate_expression("=4 * 5") == 20


def test_evaluate_division():
    assert evaluate_expression("=10 / 4") == 2.5


def test_evaluate_power():
    assert evaluate_expression("=2 ** 3") == 8


def test_evaluate_power_fractional():
    assert evaluate_expression("=4 ** 0.5") == 2.0


def test_evaluate_precedence():
    assert evaluate_expression("=2 + 3 * 4") == 14


def test_evaluate_parentheses():
    assert evaluate_expression("=(2 + 3) * 4") == 20


def test_evaluate_with_variables():
    assert evaluate_expression("={{x}} + {{y}}", {"x": 1, "y": 2}) == 3


def test_evaluate_complex_expression():
    result = evaluate_expression(
        "=round(({{var1}}/2) * ({{var2}}/2) * 3.14 * (4/3), 2)",
        {"var1": 10, "var2": 20},
    )
    assert result == 209.33


def test_evaluate_round_two_decimals():
    assert evaluate_expression("=round(3.14159, 2)") == 3.14


def test_evaluate_round_zero_decimals():
    assert evaluate_expression("=round(3.7, 0)") == 4.0


def test_evaluate_round_with_variable():
    assert evaluate_expression("=round({{val}}, 1)", {"val": 3.14159}) == 3.1


def test_evaluate_abs_positive():
    assert evaluate_expression("=abs(5)") == 5


def test_evaluate_abs_negative():
    assert evaluate_expression("=abs(-5)") == 5


def test_evaluate_abs_with_variable():
    assert evaluate_expression("=abs({{val}})", {"val": -10}) == 10


def test_evaluate_division_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        evaluate_expression("=1 / 0")


def test_evaluate_unknown_variable_raises():
    with pytest.raises(ValueError, match="unknown"):
        evaluate_expression("={{unknown}}")


def test_evaluate_string_literal_raises():
    with pytest.raises(ValueError):
        evaluate_expression('="hello"')


def test_evaluate_boolean_op_raises():
    with pytest.raises(ValueError):
        evaluate_expression("=1 and 2")


def test_evaluate_comparison_raises():
    with pytest.raises(ValueError):
        evaluate_expression("=1 > 2")


# ── ExpressionEvaluator class ──────────────────────────────────────────────


def test_evaluator_rejects_ast_module_access():
    evaluator = ExpressionEvaluator({})
    import ast
    tree = ast.parse("__import__('os')", mode="eval")
    with pytest.raises(ValueError):
        evaluator.visit(tree)


# ── process_calculations ───────────────────────────────────────────────────


def test_process_calculations_empty_returns_extracted():
    extracted = {"x": 10}
    assert process_calculations(extracted, {}) == extracted


def test_process_calculations_single():
    extracted = {"x": 10}
    calculations = {"double": "={{x}} * 2"}
    result = process_calculations(extracted, calculations)
    assert result == {"x": 10, "double": 20}


def test_process_calculations_chained():
    extracted = {"x": 10}
    calculations = {
        "double": "={{x}} * 2",
        "quadruple": "={{double}} * 2",
    }
    result = process_calculations(extracted, calculations)
    assert result == {"x": 10, "double": 20, "quadruple": 40}


def test_process_calculations_topological_order():
    extracted = {}
    calculations = {
        "c": "={{a}} + {{b}}",
        "a": "=1",
        "b": "=2",
    }
    result = process_calculations(extracted, calculations)
    assert result == {"a": 1, "b": 2, "c": 3}


def test_process_calculations_circular_raises():
    extracted = {}
    calculations = {
        "a": "={{b}}",
        "b": "={{a}}",
    }
    with pytest.raises(ValueError, match=r"(?i)circular"):
        process_calculations(extracted, calculations)


def test_process_calculations_numeric_strings_coerced():
    extracted = {"x": "10", "y": "20"}
    calculations = {"sum": "={{x}} + {{y}}"}
    result = process_calculations(extracted, calculations)
    assert result["sum"] == 30.0


def test_process_calculations_non_numeric_extracted_passthrough():
    extracted = {"name": "Alice"}
    calculations = {"doubled": "=round(3.5, 1)"}
    result = process_calculations(extracted, calculations)
    assert result == {"name": "Alice", "doubled": 3.5}


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


def test_range_inside_interval():
    got = evaluate_expression("=range({{v}}, uterus_length)", {"v": 50}, ranges=FLAT_RANGES)
    assert got == "норма"


def test_range_exact_min_belongs():
    got = evaluate_expression("=range({{v}}, uterus_length)", {"v": 40}, ranges=FLAT_RANGES)
    assert got == "норма"


def test_range_exact_max_belongs_to_next():
    got = evaluate_expression("=range({{v}}, uterus_length)", {"v": 60}, ranges=FLAT_RANGES)
    assert got == "выше"


def test_range_below_all_hits_open_min_interval():
    got = evaluate_expression("=range({{v}}, uterus_length)", {"v": 30}, ranges=FLAT_RANGES)
    assert got == "ниже"


def test_range_empty_string_value_returns_empty():
    assert evaluate_expression("=range({{v}}, uterus_length)", {"v": ""}, ranges=FLAT_RANGES) == ""


def test_range_none_value_returns_empty():
    assert evaluate_expression("=range({{v}}, uterus_length)", {"v": None}, ranges=FLAT_RANGES) == ""


def test_range_outside_every_interval_returns_empty():
    table = {"t": [{"name": "норма", "min": 40, "max": 60}]}
    assert evaluate_expression("=range({{v}}, t)", {"v": 70}, ranges=table) == ""


def test_range_value_name_not_in_variables_raises():
    with pytest.raises(ValueError, match=r"(?i)unknown variable"):
        evaluate_expression("=range({{uterus_lenght}}, uterus_length)", {}, ranges=FLAT_RANGES)


def test_range_one_arg_raises():
    with pytest.raises(ValueError, match="2 or 3 positional"):
        evaluate_expression("=range({{v}})", {"v": 1}, ranges=FLAT_RANGES)


def test_range_four_args_raises():
    with pytest.raises(ValueError, match="2 or 3 positional"):
        evaluate_expression(
            "=range({{v}}, m_echo_thickness, {{k}}, 1)",
            {"v": 10, "k": 0},
            ranges=CONDITIONAL_RANGES,
        )


def test_range_keyword_argument_raises():
    with pytest.raises(ValueError, match="2 or 3 positional"):
        evaluate_expression(
            "=range(value={{v}}, table=uterus_length)", {"v": 1}, ranges=FLAT_RANGES
        )


@pytest.mark.parametrize("literal", ['"1"', "1"])
def test_range_literal_key_raises(literal):
    with pytest.raises(ValueError, match=r"must be a \{\{variable\}\}"):
        evaluate_expression(
            f"=range({{{{v}}}}, m_echo_thickness, {literal})",
            {"v": 10},
            ranges=CONDITIONAL_RANGES,
        )


@pytest.mark.parametrize("key", [1, "1", 1.0])
def test_range_conditional_key_numeric_spellings_hit_subtable_one(key):
    got = evaluate_expression(
        "=range({{v}}, m_echo_thickness, {{k}})",
        {"v": 10, "k": key},
        ranges=CONDITIONAL_RANGES,
    )
    assert got == "критично"


def test_range_conditional_key_not_found_returns_empty_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        got = evaluate_expression(
            "=range({{v}}, m_echo_thickness, {{k}})",
            {"v": 10, "k": "фолликулярная"},
            ranges=CONDITIONAL_RANGES,
        )
    assert got == ""
    assert "m_echo_thickness" in caplog.text


def test_range_conditional_key_name_not_in_variables_raises():
    with pytest.raises(ValueError, match=r"(?i)unknown variable"):
        evaluate_expression(
            "=range({{v}}, m_echo_thickness, {{fase}})",
            {"v": 10},
            ranges=CONDITIONAL_RANGES,
        )


def test_range_conditional_key_true_hits_subtable_one():
    got = evaluate_expression(
        "=range({{v}}, m_echo_thickness, {{k}})",
        {"v": 10, "k": True},
        ranges=CONDITIONAL_RANGES,
    )
    assert got == "критично"


def test_range_key_against_flat_table_raises():
    with pytest.raises(ValueError, match=r"(?i)flat"):
        evaluate_expression(
            "=range({{v}}, uterus_length, {{k}})",
            {"v": 50, "k": 1},
            ranges=FLAT_RANGES,
        )


def test_range_missing_key_against_conditional_table_raises():
    with pytest.raises(ValueError, match=r"(?i)conditional"):
        evaluate_expression(
            "=range({{v}}, m_echo_thickness)",
            {"v": 10},
            ranges=CONDITIONAL_RANGES,
        )


def test_range_unknown_table_raises():
    with pytest.raises(ValueError, match=r"(?i)unknown ranges table"):
        evaluate_expression("=range({{v}}, nope)", {"v": 50}, ranges=FLAT_RANGES)


def test_range_unknown_table_raises_even_when_value_is_empty():
    # A config error must surface on every row, not only where the measurement is filled.
    with pytest.raises(ValueError, match=r"(?i)unknown ranges table"):
        evaluate_expression("=range({{v}}, nope)", {"v": ""}, ranges=FLAT_RANGES)


def test_range_key_against_flat_table_raises_even_when_key_is_empty():
    with pytest.raises(ValueError, match="is flat"):
        evaluate_expression(
            "=range({{v}}, uterus_length, {{k}})", {"v": "", "k": ""}, ranges=FLAT_RANGES
        )


def test_range_blank_string_value_returns_empty():
    assert evaluate_expression("=range({{v}}, uterus_length)", {"v": "  "}, ranges=FLAT_RANGES) == ""


def test_range_result_fed_back_into_arithmetic_raises():
    flag = evaluate_expression("=range({{v}}, uterus_length)", {"v": 50}, ranges=FLAT_RANGES)
    assert flag == "норма"
    with pytest.raises(ValueError, match=r"(?i)cannot be coerced"):
        evaluate_expression("={{flag}} + 1", {"flag": flag})


def test_process_calculations_range_flags_merge_into_extracted():
    extracted = {"uterus_length": 50, "m_echo_thickness": 10, "postmenopause": True}
    calculations = {
        "uterus_length_flag": "=range({{uterus_length}}, uterus_length)",
        "m_echo_flag": "=range({{m_echo_thickness}}, m_echo_thickness, {{postmenopause}})",
    }
    result = process_calculations(
        extracted, calculations, ranges={**FLAT_RANGES, **CONDITIONAL_RANGES}
    )
    assert result["uterus_length_flag"] == "норма"
    assert result["m_echo_flag"] == "критично"
