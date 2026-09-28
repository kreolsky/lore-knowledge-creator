"""Unit tests for extractor utility functions (sync, no DB) and pipeline core config."""
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipeline.core.config import (
    _extract_template_from_code_block,
    _normalize_variable_entry,
    _normalize_yaml_indentation,
    _parse_yaml_from_code_block,
    fetch_child_rows,
    parse_pipeline_config_from_doc,
    resolve_instructions_doc,
    resolve_variables_sections,
)
from pipeline.core.constants import (
    BUILTIN_PROMPT_VARS,
    DEFAULT_PROMPT,
    DEFAULT_TITLE_TEMPLATE,
)
from pipeline.extractor.utils import (
    build_json_schema,
    call_llm_structured,
    canonicalize_extracted,
    normalize_extracted,
    render_prompt,
    render_template,
    render_title_template,
)

# ── _parse_yaml_from_code_block ────────────────────────────────────────────


def test_parse_yaml_from_code_block():
    content = "```yaml\ncharacter_name: The name of the character\nlocation: Where the scene takes place\n```"
    result = _parse_yaml_from_code_block(content)
    assert result == {"character_name": "The name of the character", "location": "Where the scene takes place"}


def test_parse_yaml_no_code_block():
    content = "character_name: The name\nlocation: The place"
    result = _parse_yaml_from_code_block(content)
    assert result == {"character_name": "The name", "location": "The place"}


def test_parse_yaml_single_key_wrapper():
    content = "```yaml\nvariables:\n  patient_name: ФИО пациента\n  age: Возраст\n```"
    result = _parse_yaml_from_code_block(content)
    assert result == {"patient_name": "ФИО пациента", "age": "Возраст"}


def test_parse_yaml_single_key_non_dict_value_not_flattened():
    content = "```yaml\ntitle: Just a string\n```"
    result = _parse_yaml_from_code_block(content)
    assert result == {"title": "Just a string"}


def test_parse_yaml_multi_key_not_flattened():
    content = "```yaml\na: val_a\nb: val_b\nc: val_c\n```"
    result = _parse_yaml_from_code_block(content)
    assert result == {"a": "val_a", "b": "val_b", "c": "val_c"}


def test_parse_yaml_invalid():
    with pytest.raises(ValueError, match="dict"):
        _parse_yaml_from_code_block("- item1\n- item2")


def test_parse_yaml_merge_keys_share_a_defaults_block():
    """A defaults block declared once via an anchor is merged into every variable
    that references it with `<<: *anchor`; a per-variable key still wins."""
    content = (
        "```yaml\n"
        "_opt: &opt {type: string, default: \"-\"}\n"
        "variables:\n"
        "  size: {<<: *opt, description: размер}\n"
        "  shape: {<<: *opt, description: форма, default: \"н/д\"}\n"
        "```"
    )
    result = _parse_yaml_from_code_block(content)
    assert result["variables"] == {
        "size": {"type": "string", "default": "-", "description": "размер"},
        "shape": {"type": "string", "default": "н/д", "description": "форма"},
    }


def test_parse_yaml_merge_keys_keep_duplicate_key_detection():
    """Merge-key support does not disable the duplicate-key sink."""
    content = "_opt: &opt {type: string}\nvariables:\n  a: {<<: *opt}\n  a: {<<: *opt}\n"
    dups: list[str] = []
    _parse_yaml_from_code_block(content, dup_sink=dups)
    assert dups == ["a"]


# ── _extract_template_from_code_block ──────────────────────────────────────


def test_extract_template_from_code_block():
    content = "```markdown\n# {{title}}\n\nBody: {{body}}\n```"
    result = _extract_template_from_code_block(content)
    assert result == "# {{title}}\n\nBody: {{body}}"


def test_extract_template_no_code_block():
    content = "# {{title}}\n\nBody: {{body}}"
    result = _extract_template_from_code_block(content)
    assert result == "# {{title}}\n\nBody: {{body}}"


# ── build_json_schema ──────────────────────────────────────────────────────


def test_build_json_schema():
    variables = {"name": "Person name", "age": "Person age"}
    schema = build_json_schema(variables)
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "age" in schema["properties"]
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["name"]["description"] == "Person name"
    assert schema["required"] == ["name", "age"]
    assert schema["additionalProperties"] is False


# ── _normalize_variable_entry: kinds + default ─────────────────────────────


def test_normalize_variable_entry_plain_string():
    desc, jtype, enum, kind, default = _normalize_variable_entry("x", "just a string")
    assert (desc, jtype, enum, kind, default) == ("just a string", "string", None, None, None)


def test_normalize_variable_entry_legacy_enum_key():
    desc, jtype, enum, kind, default = _normalize_variable_entry(
        "doc_surname", {"description": "Surname", "enum": ["A", "B"]}
    )
    assert kind == "enum"
    assert enum == ["A", "B"]
    assert jtype == "string"


def test_normalize_variable_entry_type_enum():
    desc, jtype, enum, kind, default = _normalize_variable_entry(
        "f", {"description": "d", "type": "enum", "options": ["A", "B"]}
    )
    assert kind == "enum"
    assert enum == ["A", "B"]


def test_normalize_variable_entry_type_multiselect():
    desc, jtype, enum, kind, default = _normalize_variable_entry(
        "access", {"description": "d", "type": "multiselect", "options": ["x", "y", "z"]}
    )
    assert kind == "multiselect"
    assert enum == ["x", "y", "z"]


def test_normalize_variable_entry_type_prefix():
    desc, jtype, enum, kind, default = _normalize_variable_entry(
        "canal", {"description": "d", "type": "prefix", "options": ["расширен", "не расширен"]}
    )
    assert kind == "prefix"
    assert enum == ["расширен", "не расширен"]


def test_normalize_variable_entry_default():
    desc, jtype, enum, kind, default = _normalize_variable_entry(
        "f", {"description": "d", "default": "Не указано"}
    )
    assert default == "Не указано"
    assert kind is None


def test_normalize_variable_entry_rejects_enum_and_type():
    with pytest.raises(ValueError, match="mutually exclusive|both"):
        _normalize_variable_entry(
            "f", {"description": "d", "enum": ["A"], "type": "enum", "options": ["A"]}
        )


def test_normalize_variable_entry_rejects_empty_options():
    with pytest.raises(ValueError, match="options"):
        _normalize_variable_entry("f", {"description": "d", "type": "enum", "options": []})


def test_normalize_variable_entry_rejects_non_string_options():
    with pytest.raises(ValueError, match="options|strings"):
        _normalize_variable_entry("f", {"description": "d", "type": "multiselect", "options": [1, 2]})


def test_normalize_variable_entry_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown type|type"):
        _normalize_variable_entry("f", {"description": "d", "type": "bogus"})


def test_normalize_variable_entry_rejects_non_string_default():
    with pytest.raises(ValueError, match="default"):
        _normalize_variable_entry("f", {"description": "d", "default": 5})


# ── build_json_schema: kinds ────────────────────────────────────────────────


def test_build_json_schema_enum_kind_is_free_string():
    # Variant 4: enum kind emits a plain free string (no enum grammar). The `options:`
    # feed the downstream canonicalizer, not the schema.
    schema = build_json_schema(
        {"s": "d"}, kinds={"s": "enum"}, enums={"s": ["A", "B"]}
    )
    prop = schema["properties"]["s"]
    assert prop == {"type": "string", "description": "d"}
    assert "enum" not in prop


def test_build_json_schema_enum_allows_empty_escape():
    # A plain string naturally permits "" — the model can still signal "not stated"
    # (Rule 2) so default: fires via normalize_extracted.
    schema = build_json_schema({"s": "d"}, kinds={"s": "enum"}, enums={"s": ["A", "B"]})
    assert schema["properties"]["s"]["type"] == "string"
    assert "enum" not in schema["properties"]["s"]


def test_build_json_schema_prefix_kind_is_free_string():
    # Variant 4: prefix kind also emits a plain free string (no prefix/detail object).
    schema = build_json_schema(
        {"p": "d"}, kinds={"p": "prefix"}, enums={"p": ["расширен", "не расширен"]}
    )
    assert schema["properties"]["p"] == {"type": "string", "description": "d"}


def test_build_json_schema_multiselect_kind():
    schema = build_json_schema(
        {"m": "d"}, kinds={"m": "multiselect"}, enums={"m": ["x", "y"]}
    )
    prop = schema["properties"]["m"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string", "enum": ["x", "y"]}


def test_build_json_schema_legacy_enum_still_works():
    # Backward compat: enums without kinds → plain string+enum (unchanged behavior).
    schema = build_json_schema({"s": "d"}, enums={"s": ["A", "B"]})
    assert schema["properties"]["s"]["type"] == "string"
    assert schema["properties"]["s"]["enum"] == ["A", "B"]


def test_build_json_schema_number_allows_empty_escape():
    # A required number is un-emptyable like an enum; the "string" union lets the model
    # return "" (Rule 2) so default: can fire (postmenopause → 0 when unspoken).
    schema = build_json_schema({"n": "d"}, types={"n": "number"})
    assert schema["properties"]["n"]["type"] == ["number", "null"]


def test_build_json_schema_integer_allows_empty_escape():
    schema = build_json_schema({"n": "d"}, types={"n": "integer"})
    assert schema["properties"]["n"]["type"] == ["integer", "null"]


def test_build_json_schema_string_type_unchanged():
    schema = build_json_schema({"s": "d"}, types={"s": "string"})
    assert schema["properties"]["s"]["type"] == "string"


def test_normalize_extracted_number_empty_applies_default():
    # The number "" escape round-trips to the default (postmenopause unspoken → 0).
    out = normalize_extracted({"postmenopause": ""}, {}, {"postmenopause": "0"})
    assert out["postmenopause"] == "0"


def test_build_json_schema_backward_compat_identical():
    # A variable with no kind/enum produces byte-identical schema as today.
    plain = build_json_schema({"name": "Person name", "age": "Person age"})
    with_empty = build_json_schema(
        {"name": "Person name", "age": "Person age"}, kinds={}, enums={}
    )
    assert plain == with_empty


# ── canonicalize_extracted (Variant 4) ─────────────────────────────────────

DOCTOR_OPTIONS = [
    "Арсенян Г.А.", "Барановская Ю.П.", "Блохина Л.А.", "Воеводин Ф.С.",
    "Жукоцкая М.К.", "Иванова О.Д.", "Курганников А.С.", "Лункина Е.Г.",
    "Магнитская Н.А.", "Сбитнев В.В.", "Тё С.А.", "Чамеева Т.В.", "Не указано",
]


def test_canonicalize_exact_match():
    out = canonicalize_extracted(
        {"f": "правильная"}, {"f": ["правильная", "неправильная"]}, {"f": "enum"}
    )
    assert out["f"] == "правильная"


def test_canonicalize_yo_and_case_insensitive():
    out = canonicalize_extracted(
        {"d": "тё с.а."}, {"d": DOCTOR_OPTIONS}, {"d": "enum"}
    )
    # ё=е + case fold: 'тё с.а.' → the canonical 'Тё С.А.' from the DERIVED roster.
    assert out["d"] == next(o for o in DOCTOR_OPTIONS if o.lower().replace("ё", "е") == "те с.а.")


def test_canonicalize_containment_picks_longest_option():
    # 'неправильная' contains 'правильная' as a substring; the more specific (longer)
    # option must win, not the first shorter substring match.
    opts = ["правильная", "неправильная"]
    out = canonicalize_extracted({"f": "форма неправильная"}, {"f": opts}, {"f": "enum"})
    assert out["f"] == "неправильная"


def test_canonicalize_fuzzy_hit_above_threshold():
    # 'Збитник' (ASR) fuzzily maps to the roster surname 'Сбитнев В.В.'.
    out = canonicalize_extracted({"d": "Збитник"}, {"d": DOCTOR_OPTIONS}, {"d": "enum"})
    assert out["d"] == "Сбитнев В.В."


def test_canonicalize_fuzzy_miss_keeps_free_string():
    # No confident match → keep the free transcription (prod-like accuracy).
    free = "нечто совершенно постороннее"
    out = canonicalize_extracted(
        {"f": free}, {"f": ["правильная", "неправильная"]}, {"f": "enum"}
    )
    assert out["f"] == free


def test_canonicalize_empty_passes_through():
    # Empty stays empty so normalize_extracted's default: escape fires downstream.
    out = canonicalize_extracted({"f": ""}, {"f": ["правильная"]}, {"f": "enum"})
    assert out["f"] == ""


def test_canonicalize_skips_multiselect():
    # multiselect values are lists joined by normalize_extracted — never canonicalized.
    out = canonicalize_extracted(
        {"m": ["x", "y"]}, {"m": ["x", "y", "z"]}, {"m": "multiselect"}
    )
    assert out["m"] == ["x", "y"]


def test_canonicalize_field_without_options_untouched():
    out = canonicalize_extracted({"free": "любой текст"}, {}, {})
    assert out["free"] == "любой текст"


LOC_OPTIONS = [
    "определяется в anteflexio, отклонено влево",
    "определяется в anteflexio, отклонено вправо",
    "определяется в anteflexio, по средней линии",
    "определяется в retroflexio, отклонено вправо",
    "определяется в retroflexio, по средней линии",
    "прямая ось, по средней линии",
    "без сгиба, по средней линии",
]


def test_canonicalize_partial_axis_kept_free_not_fabricated():
    # A bare flexion value with no deviation spoken must NOT be snapped to a full
    # multi-axis option — that would fabricate an unspoken deviation on a medical
    # protocol. Keep the honest partial free string.
    out = canonicalize_extracted(
        {"loc": "anteflexio"}, {"loc": LOC_OPTIONS}, {"loc": "enum"}
    )
    assert out["loc"] == "anteflexio"


def test_canonicalize_partial_axis_retroflexio_kept_free():
    out = canonicalize_extracted(
        {"loc": "retroflexio"}, {"loc": LOC_OPTIONS}, {"loc": "enum"}
    )
    assert out["loc"] == "retroflexio"


def test_canonicalize_both_axes_present_snaps_to_full_option():
    # When the value covers both axes, snapping to the canonical option is correct.
    out = canonicalize_extracted(
        {"loc": "anteflexio, по средней линии"}, {"loc": LOC_OPTIONS}, {"loc": "enum"}
    )
    assert out["loc"] == "определяется в anteflexio, по средней линии"


def test_canonicalize_prefix_free_string_snaps_to_option():
    # Variant 4: prefix fields arrive as free strings; the HEAD snaps to a canonical
    # option. CONTRACT CHANGED 2026-07-27: the remainder is no longer discarded — a
    # prefix value is "option + detail" and dropping the detail deleted real findings
    # (see _canonicalize_prefix's INVARIANT and the tail tests below).
    opts = ["Определяется в типичном месте", "определяется за дном матки"]
    out = canonicalize_extracted(
        {"o": "определяется за дном матки в типичном месте"}, {"o": opts}, {"o": "prefix"}
    )
    assert out["o"].startswith("определяется за дном матки")
    assert "в типичном месте" in out["o"]


# ── normalize_extracted ─────────────────────────────────────────────────────


def test_normalize_extracted_multiselect_two():
    out = normalize_extracted({"a": ["X", "Y"]}, {"a": "multiselect"}, {})
    assert out["a"] == "X и Y"


def test_normalize_extracted_multiselect_one():
    out = normalize_extracted({"a": ["X"]}, {"a": "multiselect"}, {})
    assert out["a"] == "X"


def test_normalize_extracted_multiselect_three():
    out = normalize_extracted({"a": ["X", "Y", "Z"]}, {"a": "multiselect"}, {})
    assert out["a"] == "X, Y и Z"


def test_normalize_extracted_multiselect_empty_no_default():
    out = normalize_extracted({"a": []}, {"a": "multiselect"}, {})
    assert out["a"] == ""


_ACCESS_OPTIONS = ["трансвагинальный", "трансабдоминальный", "трансректальный"]


def test_normalize_extracted_multiselect_sorts_by_option_order():
    out = normalize_extracted(
        {"a": ["трансабдоминальный", "трансвагинальный"]},
        {"a": "multiselect"},
        {},
        enums={"a": _ACCESS_OPTIONS},
    )
    assert out["a"] == "трансвагинальный и трансабдоминальный"


def test_normalize_extracted_multiselect_order_independent():
    kwargs = ({"a": "multiselect"}, {})
    forward = normalize_extracted(
        {"a": ["трансвагинальный", "трансабдоминальный"]}, *kwargs, enums={"a": _ACCESS_OPTIONS}
    )
    reverse = normalize_extracted(
        {"a": ["трансабдоминальный", "трансвагинальный"]}, *kwargs, enums={"a": _ACCESS_OPTIONS}
    )
    assert forward["a"] == reverse["a"]


def test_normalize_extracted_multiselect_three_keeps_option_order():
    out = normalize_extracted(
        {"a": ["трансректальный", "трансабдоминальный", "трансвагинальный"]},
        {"a": "multiselect"},
        {},
        enums={"a": _ACCESS_OPTIONS},
    )
    assert out["a"] == "трансвагинальный, трансабдоминальный и трансректальный"


def test_normalize_extracted_multiselect_unknown_item_appends_last():
    out = normalize_extracted(
        {"a": ["неизвестный", "трансабдоминальный", "трансвагинальный"]},
        {"a": "multiselect"},
        {},
        enums={"a": _ACCESS_OPTIONS},
    )
    assert out["a"] == "трансвагинальный, трансабдоминальный и неизвестный"


def test_normalize_extracted_multiselect_without_enums_keeps_model_order():
    out = normalize_extracted(
        {"a": ["трансабдоминальный", "трансвагинальный"]}, {"a": "multiselect"}, {}
    )
    assert out["a"] == "трансабдоминальный и трансвагинальный"


def test_normalize_extracted_multiselect_empty_with_enums_still_empty():
    out = normalize_extracted(
        {"a": []}, {"a": "multiselect"}, {}, enums={"a": _ACCESS_OPTIONS}
    )
    assert out["a"] == ""


def test_normalize_extracted_enum_empty_applies_default():
    # The "" escape from the schema round-trips here: enum returned empty → default.
    out = normalize_extracted({"a": ""}, {"a": "enum"}, {"a": "Samsung Medison A30"})
    assert out["a"] == "Samsung Medison A30"


def test_normalize_extracted_prefix_empty_applies_default():
    out = normalize_extracted(
        {"a": {"prefix": "", "detail": ""}}, {"a": "prefix"}, {"a": "не расширен"}
    )
    assert out["a"] == "не расширен"


def test_normalize_extracted_multiselect_empty_with_default():
    out = normalize_extracted({"a": []}, {"a": "multiselect"}, {"a": "Не указано"})
    assert out["a"] == "Не указано"


def test_normalize_extracted_prefix_with_detail():
    out = normalize_extracted(
        {"p": {"prefix": "расширен", "detail": "до 2 мм"}}, {"p": "prefix"}, {}
    )
    assert out["p"] == "расширен до 2 мм"


def test_normalize_extracted_prefix_terminal():
    out = normalize_extracted(
        {"p": {"prefix": "не расширен", "detail": ""}}, {"p": "prefix"}, {}
    )
    assert out["p"] == "не расширен"


def test_normalize_extracted_default_on_empty_string():
    out = normalize_extracted({"s": ""}, {}, {"s": "Не указано"})
    assert out["s"] == "Не указано"


def test_normalize_extracted_default_not_applied_when_present():
    out = normalize_extracted({"s": "value"}, {}, {"s": "Не указано"})
    assert out["s"] == "value"


def test_normalize_extracted_plain_passthrough():
    out = normalize_extracted({"s": "hello", "n": "42"}, {}, {})
    assert out == {"s": "hello", "n": "42"}


def test_normalize_extracted_non_categorical_untouched():
    out = normalize_extracted(
        {"cat": ["A", "B"], "plain": "kept"}, {"cat": "multiselect"}, {}
    )
    assert out["cat"] == "A и B"
    assert out["plain"] == "kept"


# ── duplicate-key detection ─────────────────────────────────────────────────


def test_parse_yaml_collects_duplicate_keys():
    content = "```yaml\na: first\nb: two\na: second\n```"
    dups = []
    result = _parse_yaml_from_code_block(content, dup_sink=dups)
    assert result == {"a": "second", "b": "two"}  # last-wins preserved
    assert dups == ["a"]


def test_parse_yaml_no_duplicates():
    content = "```yaml\na: one\nb: two\n```"
    dups = []
    _parse_yaml_from_code_block(content, dup_sink=dups)
    assert dups == []


# ── render_template ────────────────────────────────────────────────────────


def test_render_template():
    template = "# {{character_name}}\n\nLocation: {{location}}\nAge: {{age}}"
    data = {"character_name": "Alice", "location": "Wonderland", "age": "25"}
    result = render_template(template, data, "doc-123", "Source Doc")
    assert "# Alice" in result
    assert "Location: Wonderland" in result
    assert "Age: 25" in result


def test_render_template_missing_key():
    template = "# {{name}}\n\n{{missing_key}}"
    data = {"name": "Bob"}
    result = render_template(template, data, "doc-1", "Title")
    assert "# Bob" in result
    assert result.count("") or "{{missing_key}}" not in result


def test_render_template_system_variables():
    template = "# {{name}}\nSource: [{{doc_title}}](doc:{{doc_id}}) ref={{ref_id}}"
    data = {"name": "Test"}
    result = render_template(template, data, "doc-42", "My Source", "ref-7")
    assert "# Test\nSource: [My Source](doc:doc-42) ref=ref-7" == result


def test_render_template_no_backlink_appended():
    template = "# {{name}}"
    data = {"name": "Test"}
    result = render_template(template, data, "doc-42", "My Source")
    assert result == "# Test"


def test_render_template_ref_title():
    template = "Reference: {{ref_title}}\n# {{name}}"
    data = {"name": "Bob"}
    result = render_template(template, data, "doc-1", "Src", "ref-9", "Interview-1")
    assert "Reference: Interview-1" in result
    assert "# Bob" in result


def test_render_template_ref_title_default_empty():
    template = "[{{ref_title}}]"
    result = render_template(template, {}, "doc-1", "Src", "ref-9")
    assert result == "[]"


# ── render_title_template ──────────────────────────────────────────────────


def test_render_title_template_default():
    result = render_title_template(None, "My Doc")
    assert "My Doc" in result
    assert re.match(r"extractor-\d{4}-\d{2}-\d{2} \d{2}:\d{2} My Doc", result)


def test_render_title_template_empty_string():
    result = render_title_template("", "My Doc")
    assert "My Doc" in result
    assert re.match(r"extractor-\d{4}-\d{2}-\d{2} \d{2}:\d{2} My Doc", result)


def test_render_title_template_whitespace():
    result = render_title_template("   ", "My Doc")
    assert "My Doc" in result
    assert re.match(r"extractor-\d{4}-\d{2}-\d{2} \d{2}:\d{2} My Doc", result)


def test_render_title_template_title_var():
    result = render_title_template("Note: {title}", "Interview")
    assert result == "Note: Interview"


def test_render_title_template_date_vars():
    result = render_title_template("{yyyy}-{mm}-{dd}_{HH}-{MM}", "Doc")
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$", result)


def test_render_title_template_rnd():
    result = render_title_template("{rnd:8}", "Doc")
    assert len(result) == 8
    assert re.match(r"^[a-zA-Z0-9]{8}$", result)


def test_render_title_template_rnd_different_each_call():
    r1 = render_title_template("{rnd:16}", "Doc")
    r2 = render_title_template("{rnd:16}", "Doc")
    assert r1 != r2


def test_render_title_template_mixed():
    result = render_title_template("{title}_{yyyy}-{mm}-{dd}_{rnd:4}", "Session")
    assert result.startswith("Session_")
    parts = result.split("_")
    assert len(parts) == 3
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", parts[1])
    assert re.match(r"^[a-zA-Z0-9]{4}$", parts[2])


def test_render_title_template_unknown_var_passthrough():
    result = render_title_template("{unknown}", "Doc")
    assert result == "{unknown}"


def test_render_title_template_custom_default():
    result = render_title_template(None, "Hello", default_template="custom-{title}")
    assert result == "custom-Hello"


def test_render_title_template_ref_title():
    result = render_title_template("{title} — {ref_title}", "Src", reference_title="Voice-42")
    assert result == "Src — Voice-42"


def test_render_title_template_ref_title_empty_when_missing():
    result = render_title_template("[{ref_title}]", "Src")
    assert result == "[]"


def test_render_title_template_doc_title():
    result = render_title_template("{title} / {doc_title}", "Source", reference_title="Ref")
    assert result == "Source / Source"


def test_render_title_template_uses_client_timezone():
    from datetime import datetime
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo
    utc_hour = datetime.now(_tz.utc).hour
    moscow_hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    if utc_hour == moscow_hour:
        pytest.skip("UTC and Moscow hour coincide (impossible — sanity guard)")
    result = render_title_template("{HH}", "Doc", tz_name="Europe/Moscow")
    assert result == f"{moscow_hour:02d}"


def test_render_title_template_bad_tz_falls_back_to_utc():
    from datetime import datetime
    from datetime import timezone as _tz
    expected_hour = datetime.now(_tz.utc).strftime("%H")
    result = render_title_template("{HH}", "Doc", tz_name="Not/AReal_Zone")
    assert result == expected_hour


# ── constants defaults ─────────────────────────────────────────────────────


def test_default_prompt_has_placeholders():
    # {variables} is load-bearing, not cosmetic: on a llama-server backend the JSON schema
    # becomes a GBNF grammar that drops every `description`, so the prompt is the only
    # channel carrying field descriptions. See INVARIANT in pipeline/core/constants.py.
    assert "{transcription}" in DEFAULT_PROMPT
    assert "{variables}" in DEFAULT_PROMPT
    assert "{instructions}" in DEFAULT_PROMPT


def test_default_prompt_has_json_instruction():
    assert "JSON" in DEFAULT_PROMPT
    assert "no markdown fences" in DEFAULT_PROMPT.lower()


def test_default_title_template():
    assert "{yyyy}" in DEFAULT_TITLE_TEMPLATE
    assert "{title}" in DEFAULT_TITLE_TEMPLATE


def test_builtin_prompt_vars():
    assert "transcription" in BUILTIN_PROMPT_VARS


# ── render_prompt ──────────────────────────────────────────────────────────


def test_render_prompt_basic():
    result = render_prompt(
        "Fields: {variables}\nTranscription: {transcription}",
        {"name": "Person name"},
        "transcript text",
        "",
        {},
    )
    assert "- `name`: Person name" in result
    assert "transcript text" in result


def test_render_prompt_with_instructions():
    result = render_prompt(
        "{instructions}\n{transcription}",
        {"name": "Name"},
        "text",
        "Use formal tone",
        {},
    )
    assert "Use formal tone" in result


def test_render_prompt_instructions_alias():
    result = render_prompt(
        "{user_instructions}\n{transcription}",
        {"name": "Name"},
        "text",
        "Be concise",
        {},
    )
    assert "Be concise" in result


def test_render_prompt_fields_alias():
    result = render_prompt(
        "{fields}\n{transcription}",
        {"age": "The age"},
        "text",
        "",
        {},
    )
    assert "- `age`: The age" in result


def test_render_prompt_child_doc_placeholder():
    result = render_prompt(
        "{glossary}\n{transcription}",
        {"name": "Name"},
        "text",
        "",
        {"glossary": "Term1: definition"},
    )
    assert "Term1: definition" in result


def test_render_prompt_default_prompt_does_not_inject_ranges_child():
    """The shipped prompt template must not leak the `ranges` doc to the model.

    The ranges doc holds reference intervals for the `range()` computed variable —
    a deterministic config input the model must never read (same posture as
    `typography`). It reaches the prompt only through an explicit `{ranges}`
    placeholder in a CUSTOM prompt.
    """
    from pipeline.core.constants import DEFAULT_PROMPT

    result = render_prompt(
        DEFAULT_PROMPT,
        {"name": "Name"},
        "text",
        "",
        {"ranges": "SENTINEL"},
    )
    assert "SENTINEL" not in result


# ── parse_pipeline_config_from_doc ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_pipeline_config_from_doc_with_yaml():
    mock_doc = {
        "content": "```yaml\nprompt: Custom prompt {transcription}\nretries:\n  max_retries: 5\n  wait: 3\n```",
    }
    with patch("pipeline.core.config.fetch_one", return_value=mock_doc):
        config = await parse_pipeline_config_from_doc("cfg-1")
    assert config["pipeline"] == "extractor"
    assert config["prompt"] == "Custom prompt {transcription}"
    assert "retries" not in config


@pytest.mark.asyncio
async def test_parse_pipeline_config_from_doc_defaults():
    mock_doc = {"content": ""}
    with patch("pipeline.core.config.fetch_one", return_value=mock_doc):
        config = await parse_pipeline_config_from_doc("cfg-2")
    assert config["pipeline"] == "extractor"
    assert config["prompt"] == DEFAULT_PROMPT
    assert set(config.keys()) == {"pipeline", "prompt"}


@pytest.mark.asyncio
async def test_parse_pipeline_config_from_doc_not_found():
    with patch("pipeline.core.config.fetch_one", return_value=None):
        with pytest.raises(ValueError, match="not found"):
            await parse_pipeline_config_from_doc("missing")


@pytest.mark.asyncio
async def test_parse_pipeline_config_from_doc_malformed_yaml_raises():
    mock_doc = {"content": "```yaml\n: : not a dict\n```"}
    with patch("pipeline.core.config.fetch_one", return_value=mock_doc):
        with pytest.raises((ValueError, Exception)):
            await parse_pipeline_config_from_doc("cfg-bad")


# ── fetch_child_rows ────────────────────────────────────────────────────────


def _make_row(title, content):
    return {"title": title, "content": content}


@pytest.mark.asyncio
async def test_fetch_child_rows():
    rows = [_make_row("template", "content")]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = await fetch_child_rows("cfg-1")
    assert result == rows
    mock_db.query.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_child_rows_empty():
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=None)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = await fetch_child_rows("cfg-1")
    assert result == []


# ── resolve_variables_doc / resolve_instructions_doc ────────────────────────


@pytest.mark.asyncio
async def test_resolve_variables_doc():
    rows = [
        _make_row("variables", "```yaml\nname: The name\nage: The age\n```"),
        _make_row("template", "```markdown\n# {{name}}\n```"),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = (await resolve_variables_sections("cfg-1"))[0]
    assert result == {"name": "The name", "age": "The age"}


@pytest.mark.asyncio
async def test_resolve_variables_doc_values_alias():
    rows = [
        _make_row("values", "```yaml\nx: X field\n```"),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = (await resolve_variables_sections("cfg-2"))[0]
    assert result == {"x": "X field"}


@pytest.mark.asyncio
async def test_resolve_variables_doc_not_found():
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=[])
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        with pytest.raises(ValueError, match="variables"):
            await resolve_variables_sections("cfg-3")


@pytest.mark.asyncio
async def test_resolve_instructions_doc():
    rows = [
        _make_row("instructions", "Use UK English spelling"),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = await resolve_instructions_doc("cfg-1")
    assert result == "Use UK English spelling"


@pytest.mark.asyncio
async def test_resolve_instructions_doc_not_found():
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=[])
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = await resolve_instructions_doc("cfg-2")
    assert result == ""


# ── call_llm_structured ────────────────────────────────────────────────────


def _make_httpx_mock(response_content: dict):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(response_content)}}]
    }
    captured = {}

    async def mock_post(url, json, headers):
        captured["payload"] = json
        return mock_resp

    mock_client = AsyncMock()
    mock_client.post = mock_post
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm, captured


@pytest.mark.asyncio
async def test_call_llm_structured_sends_prompt():
    mock_client, captured = _make_httpx_mock({"name": "Alice"})
    with patch("pipeline.extractor.utils.httpx.AsyncClient", return_value=mock_client):
        result = await call_llm_structured(
            user_prompt="Extract data from: Hello my name is Alice",
            json_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
    assert result == {"name": "Alice"}
    prompt = captured["payload"]["messages"][0]["content"]
    assert "Hello my name is Alice" in prompt


@pytest.mark.asyncio
async def test_call_llm_structured_hardcoded_response_format():
    mock_client, captured = _make_httpx_mock({"name": "Bob"})
    with patch("pipeline.extractor.utils.httpx.AsyncClient", return_value=mock_client):
        result = await call_llm_structured(
            user_prompt="test prompt",
            json_schema={"type": "object"},
        )
    assert result == {"name": "Bob"}
    rf = captured["payload"]["response_format"]["json_schema"]
    assert rf["name"] == "extraction"
    assert rf["strict"] is True


# ── Nodes: instructions flow ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_node_stores_instructions():
    from pipeline.extractor.nodes import SetupNode

    node = SetupNode()
    shared = {
        "reference_id": "ref-1",
        "source_doc_id": "src-1",
        "config_doc_id": "cfg-1",
        "target_doc_id": "tgt-1",
        "project_id": "proj-1",
    }

    mock_ref = {"content": "transcription text", "is_reference": True}
    mock_source = {"title": "Source Doc"}

    mock_config = {
        "pipeline": "extractor",
        "prompt": "{variables}\n{instructions}\n{transcription}",
    }

    with (
        patch("pipeline.extractor.nodes.fetch_one", side_effect=[mock_ref, mock_source]),
        patch("pipeline.extractor.nodes.parse_pipeline_config_from_doc", return_value=mock_config),
        patch("pipeline.extractor.nodes.fetch_child_rows", return_value=[]),
        patch("pipeline.extractor.nodes.resolve_child_docs", return_value={}),
        patch("pipeline.extractor.nodes.resolve_variables_sections", return_value=(
            {"name": "The name"}, {"name": "string"}, {}, {}, {}, {}, [], []
        )),
        patch("pipeline.extractor.nodes.resolve_template_doc", return_value="{{name}}"),
        patch("pipeline.extractor.nodes.resolve_instructions_doc", return_value="Use formal tone"),
    ):
        exec_res = await node.exec_async(await node.prep_async(shared))

    assert exec_res["instructions_string"] == "Use formal tone"
    assert exec_res["variables"] == {"name": "The name"}
    assert exec_res["refine_fields"] == []

    await node.post_async(shared, None, exec_res)
    assert shared["instructions_string"] == "Use formal tone"
    assert shared["refine_fields"] == []


@pytest.mark.asyncio
async def test_extraction_node_renders_prompt():
    from pipeline.extractor.nodes import ExtractionNode

    node = ExtractionNode()
    shared = {
        "variables": {"name": "The name"},
        "variable_types": {"name": "string"},
        "variable_enums": {},
        "transcription_text": "text",
        "model": None,
        "instructions_string": "Use formal tone",
        "pipeline_config": {"prompt": "{variables}\n{instructions}\n{transcription}"},
        "child_docs": {},
    }

    prep_res = await node.prep_async(shared)

    with patch("pipeline.extractor.nodes.call_llm_structured", return_value={"name": "Alice"}) as mock_llm:
        await node.exec_async(prep_res)

    mock_llm.assert_called_once()
    call_kwargs = mock_llm.call_args
    sent_prompt = call_kwargs.kwargs.get("user_prompt") or call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs["user_prompt"]
    assert "Use formal tone" in sent_prompt
    assert "- `name`: The name" in sent_prompt


# ── build_json_schema with types ───────────────────────────────────────────


def test_build_json_schema_with_types():
    variables = {"height": "Height in mm", "name": "Person name"}
    types = {"height": "number", "name": "string"}
    schema = build_json_schema(variables, types=types)
    # number widens to [number, "string"] for the empty escape (see INVARIANT).
    assert schema["properties"]["height"]["type"] == ["number", "null"]
    assert schema["properties"]["height"]["description"] == "Height in mm"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["name"]["description"] == "Person name"


def test_build_json_schema_default_type_string():
    variables = {"name": "Person name"}
    schema = build_json_schema(variables)
    assert schema["properties"]["name"]["type"] == "string"


def test_build_json_schema_partial_types():
    variables = {"height": "Height", "name": "Name"}
    types = {"height": "number"}
    schema = build_json_schema(variables, types=types)
    assert schema["properties"]["height"]["type"] == ["number", "null"]
    assert schema["properties"]["name"]["type"] == "string"


def test_build_json_schema_unknown_type_raises():
    variables = {"x": "desc"}
    types = {"x": "date"}
    with pytest.raises(ValueError, match="type"):
        build_json_schema(variables, types=types)


# ── build_json_schema with enums ────────────────────────────────────────────


def test_build_json_schema_with_enum():
    variables = {"role": "The user role"}
    enums = {"role": ["admin", "user"]}
    schema = build_json_schema(variables, enums=enums)
    assert schema["properties"]["role"]["enum"] == ["admin", "user"]
    assert schema["properties"]["role"]["type"] == "string"


def test_build_json_schema_enum_partial():
    variables = {"role": "role desc", "name": "name desc"}
    enums = {"role": ["admin", "user"]}
    schema = build_json_schema(variables, enums=enums)
    assert schema["properties"]["role"]["enum"] == ["admin", "user"]
    assert "enum" not in schema["properties"]["name"]


# ── _normalize_variable_entry: enum validation ─────────────────────────────


def test_normalize_variable_entry_enum_valid():
    desc, typ, enum_vals, kind, default = _normalize_variable_entry(
        "role", {"description": "The role", "enum": ["a", "b"]}
    )
    assert desc == "The role"
    assert typ == "string"
    assert enum_vals == ["a", "b"]
    assert kind == "enum"


def test_normalize_variable_entry_enum_with_type_now_rejected():
    # Legacy enum: key and explicit type: are mutually exclusive (decided 2026-07-22).
    with pytest.raises(ValueError, match="mutually exclusive|both"):
        _normalize_variable_entry(
            "level", {"description": "level", "type": "integer", "enum": ["1", "2", "3"]}
        )


def test_normalize_variable_entry_enum_must_be_nonempty_list():
    with pytest.raises(ValueError, match="enum"):
        _normalize_variable_entry("role", {"enum": []})


def test_normalize_variable_entry_enum_items_must_be_strings():
    with pytest.raises(ValueError, match="enum"):
        _normalize_variable_entry("role", {"enum": ["a", 1]})


def test_normalize_variable_entry_enum_non_list_raises():
    with pytest.raises(ValueError, match="enum"):
        _normalize_variable_entry("role", {"enum": "admin"})


# ── resolve_variables_sections: enum extraction from typed YAML ────────────


@pytest.mark.asyncio
async def test_resolve_variables_sections_extracts_enum():
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  role:
    description: The user role
    enum: [admin, user, guest]
  name: The name
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        variables, types, calculations, enums, kinds, defaults, duplicates, refine = \
            await resolve_variables_sections("cfg-1")

    assert variables["role"] == "The user role"
    assert types["role"] == "string"
    assert enums == {"role": ["admin", "user", "guest"]}
    assert kinds == {"role": "enum"}  # legacy enum: key normalizes to kind=enum
    assert calculations == {}
    assert duplicates == []
    assert refine == []


# ── resolve_variables_sections: refine section ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_variables_sections_returns_refine_passes():
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  localization_uterus: Положение матки
  size: Размер в мм
refine:
  - localization_uterus
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        (
            variables, types, calculations, enums, kinds, defaults, duplicates, refine,
        ) = await resolve_variables_sections("cfg-1")

    assert refine == [{"fields": ["localization_uterus"], "prompt": None}]
    assert "localization_uterus" in variables


@pytest.mark.asyncio
async def test_resolve_variables_sections_no_refine_defaults_empty():
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  size: Размер в мм
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        (
            variables, types, calculations, enums, kinds, defaults, duplicates, refine,
        ) = await resolve_variables_sections("cfg-1")

    assert refine == []


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_unknown_name_fails_loud():
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  size: Размер в мм
refine:
  - typo_field
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        with pytest.raises(ValueError, match="typo_field"):
            await resolve_variables_sections("cfg-1")


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_calculate_output_dropped(caplog):
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  width: Ширина в мм
  depth: Глубина в мм
calculate:
  area: ={{width}} * {{depth}}
refine:
  - area
  - width
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        (
            variables, types, calculations, enums, kinds, defaults, duplicates, refine,
        ) = await resolve_variables_sections("cfg-1")

    assert refine == [{"fields": ["width"], "prompt": None}]
    assert "area" in caplog.text


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_pass_dict_keeps_prompt():
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  uterus: Положение матки
  ovary_right: Правый яичник
refine:
  - fields: [uterus]
    prompt: |
      Числа диктуются словами. Серия: длина, толщина, ширина.
      {variables}
      {transcription}
  - fields: [ovary_right]
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        (
            variables, types, calculations, enums, kinds, defaults, duplicates, refine,
        ) = await resolve_variables_sections("cfg-1")

    assert refine == [
        {
            "fields": ["uterus"],
            "prompt": (
                "Числа диктуются словами. Серия: длина, толщина, ширина.\n"
                "{variables}\n{transcription}\n"
            ),
        },
        {"fields": ["ovary_right"], "prompt": None},
    ]


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_pass_prompt_without_transcription_raises():
    """A pass prompt without {transcription} extracts from nothing — raise at resolve."""
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  width: Ширина в мм
refine:
  - fields: [width]
    prompt: "Extract {variables} and nothing else."
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        with pytest.raises(ValueError, match="transcription"):
            await resolve_variables_sections("cfg-1")


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_non_list_fails_loud():
    from pipeline.core.config import resolve_variables_sections

    rows = [
        _make_row(
            "variables",
            """```yaml
variables:
  size: Размер в мм
refine: localization_uterus
```""",
        ),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        with pytest.raises(ValueError, match="refine"):
            await resolve_variables_sections("cfg-1")


# ── config resolution: calculate section ───────────────────────────────────


def test__parse_yaml_variables_with_calculate():
    content = """```yaml
variables:
  name: The name
  age: The age
calculate:
  double_age: ={{age}} * 2
```"""
    result = _parse_yaml_from_code_block(content)
    assert "variables" in result
    assert "calculate" in result
    assert result["variables"]["name"] == "The name"
    assert result["calculate"]["double_age"] == "={{age}} * 2"


def test__parse_yaml_variables_backward_compat():
    content = """```yaml
patient_name: ФИО пациента
age: Возраст
```"""
    result = _parse_yaml_from_code_block(content)
    assert result == {"patient_name": "ФИО пациента", "age": "Возраст"}


def test__parse_yaml_variables_typed_flattened():
    content = """```yaml
variables:
  height:
    description: "Высота в мм"
    type: number
  name: Имя
```"""
    result = _parse_yaml_from_code_block(content)
    assert result == {
        "height": {"description": "Высота в мм", "type": "number"},
        "name": "Имя",
    }


def test__parse_yaml_with_backticks_in_value():
    content = """```yaml
patient_name: "ФИО. Если не названо, верни 'Иванова'"
uterus_position: "Положение матки. Если не в норме — вставь `⛔`"
description: |
  Многострочное
  описание с ``` бэктиками внутри.
```"""
    result = _parse_yaml_from_code_block(content)
    assert result["patient_name"] == "ФИО. Если не названо, верни 'Иванова'"
    assert "`⛔`" in str(result["uterus_position"])
    assert "описание" in result["description"]


# ── _normalize_variable_entry ──────────────────────────────────────────────


def test_normalize_variable_entry_string():
    desc, typ, enum_vals, kind, default = _normalize_variable_entry("name", "Person name")
    assert desc == "Person name"
    assert typ == "string"
    assert enum_vals is None
    assert kind is None
    assert default is None


def test_normalize_variable_entry_dict_full():
    desc, typ, enum_vals, kind, default = _normalize_variable_entry("height", {"description": "Height in mm", "type": "number"})
    assert desc == "Height in mm"
    assert typ == "number"
    assert enum_vals is None


def test_normalize_variable_entry_dict_no_type():
    desc, typ, enum_vals, kind, default = _normalize_variable_entry("name", {"description": "The name"})
    assert desc == "The name"
    assert typ == "string"
    assert enum_vals is None


def test_normalize_variable_entry_dict_empty_description():
    desc, typ, enum_vals, kind, default = _normalize_variable_entry("x", {})
    assert desc == ""
    assert typ == "string"
    assert enum_vals is None


def test_normalize_variable_entry_invalid_type_raises():
    with pytest.raises(ValueError):
        _normalize_variable_entry("x", 42)


# ── resolve_variable_types (async, no DB) ──────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_variable_types_from_typed_yaml():
    rows = [
        _make_row("variables", """```yaml
variables:
  height:
    description: Height
    type: number
  name: The name
```"""),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = (await resolve_variables_sections("cfg-1"))[1]
    assert result == {"height": "number", "name": "string"}


@pytest.mark.asyncio
async def test_resolve_variable_types_from_bare_yaml():
    rows = [
        _make_row("variables", """```yaml
patient_name: ФИО
age: Возраст
```"""),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = (await resolve_variables_sections("cfg-1"))[1]
    assert result == {"patient_name": "string", "age": "string"}


@pytest.mark.asyncio
async def test_resolve_variable_types_not_found():
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=[])
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        with pytest.raises(ValueError, match="variables"):
            await resolve_variables_sections("cfg-1")


# ── resolve_calculate_doc (async, no DB) ────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_calculate_doc_with_section():
    rows = [
        _make_row("variables", """```yaml
variables:
  name: The name
calculate:
  double: ={{name}} * 2
  triple: ={{name}} * 3
```"""),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = (await resolve_variables_sections("cfg-1"))[2]
    assert result == {"double": "={{name}} * 2", "triple": "={{name}} * 3"}


@pytest.mark.asyncio
async def test_resolve_calculate_doc_no_section():
    rows = [
        _make_row("variables", """```yaml
var1: desc
var2: desc
```"""),
    ]
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=rows)
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        result = (await resolve_variables_sections("cfg-1"))[2]
    assert result == {}


@pytest.mark.asyncio
async def test_resolve_calculate_doc_not_found():
    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=[])
    with patch("pipeline.core.config.get_db", return_value=mock_db):
        with pytest.raises(ValueError, match="variables"):
            await resolve_variables_sections("cfg-1")


# ── ComputeNode ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_node_merges_calculations():
    from pipeline.extractor.nodes import ComputeNode

    node = ComputeNode()
    shared = {
        "extracted_data": {"x": 10, "y": 20},
        "calculations": {"sum": "={{x}} + {{y}}"},
    }
    prep = await node.prep_async(shared)
    exec_res = await node.exec_async(prep)
    assert exec_res == {"x": 10, "y": 20, "sum": 30}

    await node.post_async(shared, prep, exec_res)
    assert shared["extracted_data"] == {"x": 10, "y": 20, "sum": 30}


@pytest.mark.asyncio
async def test_compute_node_empty_calculations_passthrough():
    from pipeline.extractor.nodes import ComputeNode

    node = ComputeNode()
    shared = {
        "extracted_data": {"name": "Alice", "age": "30"},
        "calculations": {},
    }
    prep = await node.prep_async(shared)
    exec_res = await node.exec_async(prep)
    assert exec_res == {"name": "Alice", "age": "30"}

    await node.post_async(shared, prep, exec_res)
    assert shared["extracted_data"] == {"name": "Alice", "age": "30"}


# ── RefineNode ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refine_node_no_section_no_llm_call():
    from pipeline.extractor.nodes import RefineNode

    node = RefineNode()
    shared = {
        "refine_fields": [],
        "variables": {"name": "The name"},
        "variable_types": {},
        "variable_enums": {},
        "variable_kinds": {},
        "transcription_text": "text",
        "model": None,
        "instructions_string": "",
        "pipeline_config": {"prompt": "{variables}\n{transcription}"},
        "child_docs": {},
        "extracted_data": {"name": "Alice"},
    }

    with patch("pipeline.extractor.nodes.call_llm_structured") as mock_llm:
        prep = await node.prep_async(shared)
        exec_res = await node.exec_async(prep)
        await node.post_async(shared, prep, exec_res)

    mock_llm.assert_not_called()
    assert exec_res is None
    assert shared["extracted_data"] == {"name": "Alice"}


@pytest.mark.asyncio
async def test_refine_node_schema_narrowed_prompt_equals_first_pass():
    from pipeline.extractor.nodes import RefineNode
    from pipeline.extractor.utils import render_prompt

    node = RefineNode()
    shared = {
        "refine_fields": ["uterus"],
        "variables": {"uterus": "Положение матки", "size": "Размер в мм"},
        "variable_types": {"uterus": "string", "size": "string"},
        "variable_enums": {},
        "variable_kinds": {},
        "transcription_text": "Матка в антефлексио.",
        "model": "local/orange/chat",
        "instructions_string": "Use formal tone",
        "pipeline_config": {"prompt": "{variables}\n{instructions}\n{transcription}"},
        "child_docs": {},
    }

    captured = {}

    async def fake_llm(user_prompt, json_schema, model=None):
        captured["user_prompt"] = user_prompt
        captured["json_schema"] = json_schema
        captured["model"] = model
        return {"uterus": "антефлексио"}

    with patch("pipeline.extractor.nodes.call_llm_structured", side_effect=fake_llm) as mock_llm:
        prep = await node.prep_async(shared)
        exec_res = await node.exec_async(prep)
        await node.post_async(shared, prep, exec_res)

    mock_llm.assert_awaited_once()
    assert list(captured["json_schema"]["properties"].keys()) == ["uterus"]
    assert captured["json_schema"]["required"] == ["uterus"]
    assert captured["model"] == "local/orange/chat"
    expected_prompt = render_prompt(
        shared["pipeline_config"]["prompt"], shared["variables"],
        shared["transcription_text"], shared["instructions_string"], shared["child_docs"],
    )
    assert captured["user_prompt"] == expected_prompt
    assert "- `size`" in captured["user_prompt"]


@pytest.mark.asyncio
async def test_refine_node_pass_prompt_renders_only_own_fields():
    """A pass with its own prompt renders ONLY that template with ONLY its fields.

    {instructions} is absent because the author did not write the placeholder —
    the pass prompt replaces the first pass's template entirely.
    """
    from pipeline.extractor.nodes import RefineNode
    from pipeline.extractor.utils import render_prompt

    node = RefineNode()
    pass_prompt = (
        "The dictated numbers come as a series: length, thickness, width.\n"
        "Extract only these fields:\n{variables}\n\nTranscription:\n{transcription}"
    )
    shared = {
        "refine_fields": [{"fields": ["uterus_size"], "prompt": pass_prompt}],
        "variables": {
            "uterus_size": "УЗИ-размеры матки (длина, толщина, ширина)",
            "size": "Размер в мм",
        },
        "variable_types": {"uterus_size": "string", "size": "string"},
        "variable_enums": {},
        "variable_kinds": {},
        "transcription_text": "Матка пятьдесят на сорок на тридцать пять.",
        "model": "local/orange/chat",
        "instructions_string": "Use formal tone",
        "pipeline_config": {"prompt": "{variables}\n{instructions}\n{transcription}"},
        "child_docs": {},
    }

    captured = {}

    async def fake_llm(user_prompt, json_schema, model=None):
        captured["user_prompt"] = user_prompt
        return {"uterus_size": "50х40х35"}

    with patch("pipeline.extractor.nodes.call_llm_structured", side_effect=fake_llm) as mock_llm:
        prep = await node.prep_async(shared)
        exec_res = await node.exec_async(prep)
        await node.post_async(shared, prep, exec_res)

    mock_llm.assert_awaited_once()
    assert captured["user_prompt"] == render_prompt(
        pass_prompt, {"uterus_size": shared["variables"]["uterus_size"]},
        shared["transcription_text"], shared["instructions_string"], shared["child_docs"],
    )
    assert "- `uterus_size`" in captured["user_prompt"]
    assert "- `size`" not in captured["user_prompt"]
    assert "Use formal tone" not in captured["user_prompt"]


@pytest.mark.asyncio
async def test_refine_node_two_passes_two_llm_calls_disjoint_schemas():
    from pipeline.extractor.nodes import RefineNode

    node = RefineNode()
    shared = {
        "refine_fields": [
            {"fields": ["uterus"], "prompt": "{variables}\n{transcription}"},
            {"fields": ["ovary_right"], "prompt": None},
        ],
        "variables": {"uterus": "Положение матки", "ovary_right": "Правый яичник"},
        "variable_types": {"uterus": "string", "ovary_right": "string"},
        "variable_enums": {},
        "variable_kinds": {},
        "transcription_text": "Матка в антефлексио, правый яичник без особенностей.",
        "model": None,
        "instructions_string": "",
        "pipeline_config": {"prompt": "{variables}\n{transcription}"},
        "child_docs": {},
        "extracted_data": {"uterus": "старое", "ovary_right": "старое"},
    }

    calls = []

    async def fake_llm(user_prompt, json_schema, model=None):
        calls.append({"user_prompt": user_prompt, "json_schema": json_schema})
        return {name: "v-" + name for name in json_schema["properties"]}

    with patch("pipeline.extractor.nodes.call_llm_structured", side_effect=fake_llm) as mock_llm:
        prep = await node.prep_async(shared)
        exec_res = await node.exec_async(prep)
        await node.post_async(shared, prep, exec_res)

    mock_llm.assert_awaited()
    assert mock_llm.await_count == 2
    assert list(calls[0]["json_schema"]["properties"].keys()) == ["uterus"]
    assert list(calls[1]["json_schema"]["properties"].keys()) == ["ovary_right"]
    assert not (
        set(calls[0]["json_schema"]["properties"])
        & set(calls[1]["json_schema"]["properties"])
    )
    assert shared["extracted_data"]["uterus"] == "v-uterus"
    assert shared["extracted_data"]["ovary_right"] == "v-ovary_right"


@pytest.mark.asyncio
async def test_refine_node_replaces_keys_with_normalized_values():
    from pipeline.extractor.nodes import RefineNode

    node = RefineNode()
    shared = {
        "refine_fields": ["doctor", "note"],
        "variables": {"doctor": "Врач", "note": "Примечание", "other": "Другое"},
        "variable_types": {"doctor": "string", "note": "string", "other": "string"},
        "variable_enums": {"doctor": ["Иванов", "Петров"]},
        "variable_kinds": {"doctor": "enum"},
        "variable_defaults": {"note": "не указано"},
        "transcription_text": "текст",
        "model": None,
        "instructions_string": "",
        "pipeline_config": {"prompt": "{transcription}"},
        "child_docs": {},
        "extracted_data": {"doctor": "Петров", "note": "первое значение", "other": "keep"},
    }

    async def fake_llm(user_prompt, json_schema, model=None):
        return {"doctor": "иванов", "note": ""}

    with patch("pipeline.extractor.nodes.call_llm_structured", side_effect=fake_llm):
        prep = await node.prep_async(shared)
        exec_res = await node.exec_async(prep)
        await node.post_async(shared, prep, exec_res)

    assert shared["extracted_data"]["doctor"] == "Иванов"
    assert shared["extracted_data"]["note"] == "не указано"
    assert shared["extracted_data"]["other"] == "keep"


@pytest.mark.asyncio
async def test_refine_node_llm_failure_keeps_first_pass(caplog):
    from pipeline.extractor.nodes import RefineNode

    node = RefineNode()
    shared = {
        "refine_fields": ["doctor"],
        "variables": {"doctor": "Врач"},
        "variable_types": {"doctor": "string"},
        "variable_enums": {},
        "variable_kinds": {},
        "transcription_text": "текст",
        "model": None,
        "instructions_string": "",
        "pipeline_config": {"prompt": "{transcription}"},
        "child_docs": {},
        "extracted_data": {"doctor": "Первый проход"},
    }

    async def boom(*args, **kwargs):
        raise RuntimeError("llm down")

    with patch("pipeline.extractor.nodes.call_llm_structured", side_effect=boom):
        prep = await node.prep_async(shared)
        exec_res = await node.exec_async(prep)
        await node.post_async(shared, prep, exec_res)

    assert shared["extracted_data"] == {"doctor": "Первый проход"}
    assert "RefineNode" in caplog.text
    assert "llm down" in caplog.text


# ── Extractor flow wiring ───────────────────────────────────────────────────


def test_create_extractor_flow_places_refine_between_extraction_and_compute():
    from pipeline.extractor.flow import create_extractor_flow
    from pipeline.extractor.nodes import (
        ComputeNode,
        ExtractionNode,
        RefineNode,
        RenderNode,
        SetupNode,
    )

    flow = create_extractor_flow()
    assert isinstance(flow.start_node, SetupNode)
    assert isinstance(flow.start_node.successors["default"], ExtractionNode)
    refine = flow.start_node.successors["default"].successors["default"]
    assert isinstance(refine, RefineNode)
    assert isinstance(refine.successors["default"], ComputeNode)
    assert isinstance(refine.successors["default"].successors["default"], RenderNode)


@pytest.mark.asyncio
async def test_compute_node_variables_with_types_flow():
    from pipeline.extractor.nodes import SetupNode

    node = SetupNode()
    shared = {
        "reference_id": "ref-1",
        "source_doc_id": "src-1",
        "config_doc_id": "cfg-1",
        "target_doc_id": "tgt-1",
        "project_id": "proj-1",
    }

    mock_ref = {"content": "transcription text", "is_reference": True}
    mock_source = {"title": "Source Doc"}
    mock_config = {"pipeline": "extractor", "prompt": "{variables}\n{transcription}"}

    with (
        patch("pipeline.extractor.nodes.fetch_one", side_effect=[mock_ref, mock_source]),
        patch("pipeline.extractor.nodes.parse_pipeline_config_from_doc", return_value=mock_config),
        patch("pipeline.extractor.nodes.fetch_child_rows", return_value=[]),
        patch("pipeline.extractor.nodes.resolve_child_docs", return_value={}),
        patch("pipeline.extractor.nodes.resolve_variables_sections", return_value=(
            {"height": "Height in mm", "width": "Width in mm"},
            {"height": "number", "width": "number"},
            {"area": "={{height}} * {{width}}"},
            {}, {}, {}, [], [],
        )),
        patch("pipeline.extractor.nodes.resolve_template_doc", return_value="{{height}}x{{width}} = {{area}}"),
        patch("pipeline.extractor.nodes.resolve_instructions_doc", return_value=""),
    ):
        exec_res = await node.exec_async(await node.prep_async(shared))

    assert exec_res["variables"] == {"height": "Height in mm", "width": "Width in mm"}
    assert exec_res["variable_types"] == {"height": "number", "width": "number"}
    assert exec_res["calculations"] == {"area": "={{height}} * {{width}}"}


# ── _normalize_yaml_indentation ─────────────────────────────────────────────


def test_normalize_yaml_indentation_pure_tabs():
    yaml_str = "key:\n\t\tsub: value\n\t\tlist:\n\t\t\t- item1"
    result = _normalize_yaml_indentation(yaml_str)
    assert "\t" not in result
    assert result == "key:\n        sub: value\n        list:\n            - item1"


def test_normalize_yaml_indentation_mixed():
    yaml_str = "key:\n\t  sub: value\n  \titem: data"
    result = _normalize_yaml_indentation(yaml_str)
    assert "\t" not in result
    assert "    " in result


def test_normalize_yaml_indentation_tab_in_quoted_value():
    yaml_str = 'key: "value\there"\nname: test'
    result = _normalize_yaml_indentation(yaml_str)
    assert 'value\there' in result


def test_normalize_yaml_indentation_no_tabs():
    yaml_str = "key:\n    sub: value\n    list:\n        - item1"
    result = _normalize_yaml_indentation(yaml_str)
    assert result == yaml_str


def test__parse_yaml_with_tab_indentation():
    content = "```yaml\nvariables:\n\tpatient_name: ФИО\n\tage: Возраст\n```"
    result = _parse_yaml_from_code_block(content)
    assert result == {"patient_name": "ФИО", "age": "Возраст"}


def test__parse_yaml_with_tabs_backward_compat():
    content = "```yaml\nkey: value\nnested:\n  sub: 42\n```"
    result = _parse_yaml_from_code_block(content)
    assert result == {"key": "value", "nested": {"sub": 42}}


# ── _parse_yaml_from_code_block: rich error messages ────────────────────────


def test_parse_yaml_invalid_block_mapping_error_contains_line_number():
    content = """```yaml
variables:
  patient_name: "ФИО"
  doc_surname: "Имя врача"
    type: string
```"""
    with pytest.raises(ValueError) as exc_info:
        _parse_yaml_from_code_block(content, doc_label="variables document var-1")
    msg = str(exc_info.value)
    assert "line" in msg.lower()
    assert "var-1" in msg


def test_parse_yaml_invalid_error_shows_snippet():
    content = """```yaml
key1: value1
key2: value2
  bad_indent: oops
```"""
    with pytest.raises(ValueError) as exc_info:
        _parse_yaml_from_code_block(content, doc_label="config document cfg-42")
    msg = str(exc_info.value)
    assert "cfg-42" in msg
    assert "bad_indent" in msg


def test_parse_yaml_invalid_no_doc_label_still_works():
    content = "```yaml\n: : broken\n```"
    with pytest.raises(ValueError):
        _parse_yaml_from_code_block(content)


def test_parse_yaml_invalid_non_yaml_error_passthrough():
    with pytest.raises(ValueError, match="dict"):
        _parse_yaml_from_code_block("- item1\n- item2")


# ── canonicalize: prefix tail + negation guard (CIR 2026-07-27) ─────────────

CANAL_OPTIONS = ["расширен", "не расширен"]


def test_canonicalize_prefix_keeps_the_tail():
    """A prefix field is 'option + detail' — canonicalizing must not eat the detail.

    Measured on CIR: 'не расширен, в просвете полип 11х4х7мм' collapsed to a bare
    'не расширен', silently deleting a finding the field's own description demands
    ('расширение И всё содержимое просвета').
    """
    value = "не расширен, в просвете определяется полип размерами 11 х 4 х 7 мм"
    out = canonicalize_extracted(
        {"cervical_canal": value}, {"cervical_canal": CANAL_OPTIONS},
        {"cervical_canal": "prefix"},
    )
    got = out["cervical_canal"]
    assert got.startswith("не расширен")
    assert "полип" in got and "11" in got, f"tail deleted: {got!r}"


def test_canonicalize_prefix_bare_option_stays_bare():
    out = canonicalize_extracted(
        {"c": "не расширен"}, {"c": CANAL_OPTIONS}, {"c": "prefix"},
    )
    assert out["c"] == "не расширен"


def test_canonicalize_prefix_head_is_canonicalized():
    # ASR/case noise in the head still snaps to the canonical option spelling.
    out = canonicalize_extracted(
        {"c": "Не Расширен, киста эндоцервикса 3 мм"}, {"c": CANAL_OPTIONS},
        {"c": "prefix"},
    )
    assert out["c"].startswith("не расширен")
    assert "киста" in out["c"]


def test_canonicalize_never_flips_a_negation():
    """A 2-character negation difference must never be folded away.

    'в нетипичном месте' vs 'в типичном месте' scores ~0.96 on any string ratio —
    higher than every threshold — yet they are opposite findings.
    """
    opts = ["Определяется в типичном месте", "Определяется позади матки"]
    free = "определяется в нетипичном месте"
    out = canonicalize_extracted({"o": free}, {"o": opts}, {"o": "enum"})
    assert out["o"] == free, "negation folded into its opposite"


def test_canonicalize_negation_guard_keeps_matching_negations():
    # Both sides negated → the guard must NOT block a legitimate snap.
    out = canonicalize_extracted(
        {"u": "не увеличен"}, {"u": ["увеличены", "не увеличены"]}, {"u": "enum"}
    )
    assert out["u"] == "не увеличены"


def test_canonicalize_negation_guard_blocks_bare_positive_option():
    # Only the positive option exists; the negated value must stay free, not snap.
    out = canonicalize_extracted(
        {"u": "не деформированы"}, {"u": ["деформированы"]}, {"u": "enum"}
    )
    assert out["u"] == "не деформированы"


# ── apply_dictionary: config-driven output typography (CIR C5, 2026-07-27) ──

def test_apply_dictionary_folds_spelled_out_units():
    from pipeline.extractor.utils import apply_dictionary
    out = apply_dictionary(
        {"f": "до 6 штук в объеме", "g": "толщина 8 миллиметров"},
        {"штук": "шт.", "миллиметров": "мм"},
    )
    assert out["f"] == "до 6 шт. в объеме"
    assert out["g"] == "толщина 8 мм"


def test_apply_dictionary_is_case_insensitive_and_word_bounded():
    from pipeline.extractor.utils import apply_dictionary
    out = apply_dictionary(
        {"a": "Миллиметров пять", "b": "миллиметровка"},  # 'миллиметровка' is NOT a unit
        {"миллиметров": "мм"},
    )
    assert out["a"] == "мм пять"
    assert out["b"] == "миллиметровка", "matched inside a longer word"


def test_apply_dictionary_leaves_numbers_and_non_strings_alone():
    from pipeline.extractor.utils import apply_dictionary
    out = apply_dictionary({"n": 8, "l": ["штук"], "s": ""}, {"штук": "шт."})
    assert out["n"] == 8 and out["l"] == ["штук"] and out["s"] == ""


def test_apply_dictionary_empty_table_is_noop():
    from pipeline.extractor.utils import apply_dictionary
    data = {"f": "до 6 штук"}
    assert apply_dictionary(data, {}) == data
    assert apply_dictionary(data, None) == data


# ── E4: numeral folding + numeral-subset guard in the canonicalizer (CIR, 2026-07-27) ──

# The CLEAN option list — no word form inlined. Inlining «(первой)/(второй)» into the
# options is the config-side workaround E4 replaces; testing against it would assert the
# workaround, not the folding.
CLEAN_PHASE_OPTIONS = [
    "1 фазе менструального цикла",
    "2 фазе менструального цикла",
]

def test_canonicalize_word_numeral_snaps_to_its_own_option():
    """«второй фазе…» must reach the SECOND option, not the first.

    difflib scores «второй фазе менструального цикла» identically (0.881) against both
    phase options, so without folding the winner is iteration order — always option 1.
    That is a silent swap of the cycle phase in a medical conclusion.
    """
    opts = CLEAN_PHASE_OPTIONS
    out = canonicalize_extracted(
        {"c": "второй фазе менструального цикла"}, {"c": opts}, {"c": "enum"}
    )
    assert out["c"] == "2 фазе менструального цикла"


def test_canonicalize_word_numeral_first_phase_still_snaps():
    opts = CLEAN_PHASE_OPTIONS
    out = canonicalize_extracted(
        {"c": "первой фазе менструального цикла"}, {"c": opts}, {"c": "enum"}
    )
    assert out["c"] == "1 фазе менструального цикла"


def test_canonicalize_numeral_guard_blocks_wrong_scale_code():
    # {3} ⊄ {2} → the option must be filtered, the value stays free.
    free = "O-RADS 2"
    out = canonicalize_extracted({"o": free}, {"o": ["O-RADS 3"]}, {"o": "enum"})
    assert out["o"] == free


def test_canonicalize_numeral_guard_is_directional_prefix_tail_survives():
    """Value-side extra numerals must NOT filter an option.

    _canonicalize_prefix routes the head through _canonicalize_one; a prefix tail
    legitimately carries measurements the option never mentions.
    """
    out = canonicalize_extracted(
        {"u": {"prefix": "не расширен, полип 11х4х7мм", "detail": ""}},
        {"u": ["не расширен"]},
        {"u": "prefix"},
    )
    assert out["u"]["prefix"].startswith("не расширен")


def test_canonicalize_numeral_lost_by_asr_keeps_free_string():
    """No numeral in the value → both phase options are filtered, nothing is invented."""
    opts = CLEAN_PHASE_OPTIONS
    free = "фазе менструального цикла"
    out = canonicalize_extracted({"c": free}, {"c": opts}, {"c": "enum"})
    assert out["c"] == free


def test_canon_norm_leaves_dimensions_alone():
    """A digit inside a dimension is data, not a numeral word.

    The MIDDLE number of a spaced dimension is reachable from neither end guard (22 is
    out of the 1–12 range, 9 precedes a unit), so the separator is checked on both sides.
    """
    from pipeline.extractor.utils import _canon_norm

    assert "двенадцат" not in _canon_norm("размерами 22 х 12 х 9 мм")
    assert "11х4х7мм" in _canon_norm("не расширен, полип 11х4х7мм")
    assert "четверт" not in _canon_norm("до 4,3мм")
    # …while a standalone ordinal still folds.
    assert "четверт" in _canon_norm("тип 4 по FIGO")


def test_canon_norm_does_not_swallow_lookalike_medical_words():
    """A numeral STEM must not capture ordinary vocabulary that starts the same way.

    'первичный'/'вторичный' (primary/secondary) and 'пятно' (a spot) are not numerals;
    folding them would let two unrelated words merge on the same stem.
    """
    from pipeline.extractor.utils import _canon_norm

    assert _canon_norm("первичная аменорея") == "первичная аменорея"
    assert _canon_norm("вторичные изменения") == "вторичные изменения"
    assert _canon_norm("гиперэхогенное пятно") == "гиперэхогенное пятно"
    # …while real ordinals and cardinals still fold.
    assert _canon_norm("второй фазе") == "втор фазе"
    assert _canon_norm("до семи мм") == "до седьм мм"


def test_canonicalize_numeral_guard_tolerates_restated_numeral_in_option():
    """An option may restate the same numeral in two notations — «2 (второй) фазе».

    Folding counts it twice ('втор втор'); the value from speech carries it once. The
    guard must compare WHICH numerals appear, not how many times — restating one numeral
    in digit + word form is a formatting choice, not a different claim.
    """
    opts = [
        "1 (первой) фазе менструального цикла",
        "2 (второй) фазе менструального цикла",
    ]
    out = canonicalize_extracted(
        {"c": "второй фазе менструального цикла"}, {"c": opts}, {"c": "enum"}
    )
    assert out["c"] == "2 (второй) фазе менструального цикла"
