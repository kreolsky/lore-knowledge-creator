"""Unit tests for extractor utility functions (sync, no DB) and pipeline core config."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pipeline.core.config import (
    _parse_yaml_from_code_block,
    parse_pipeline_config_from_doc,
    resolve_instructions_doc,
    resolve_variables_sections,
)
from pipeline.extractor.utils import (
    build_json_schema,
    call_llm_structured,
    canonicalize_extracted,
    normalize_extracted,
    render_template,
    render_title_template,
)

# ── _parse_yaml_from_code_block ────────────────────────────────────────────


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


# ── build_json_schema: kinds ────────────────────────────────────────────────


def test_build_json_schema_multiselect_kind():
    schema = build_json_schema(
        {"m": "d"}, kinds={"m": "multiselect"}, enums={"m": ["x", "y"]}
    )
    prop = schema["properties"]["m"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "string", "enum": ["x", "y"]}


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


# ── canonicalize_extracted (Variant 4) ─────────────────────────────────────

DOCTOR_OPTIONS = [
    "Арсенян Г.А.", "Барановская Ю.П.", "Блохина Л.А.", "Воеводин Ф.С.",
    "Жукоцкая М.К.", "Иванова О.Д.", "Курганников А.С.", "Лункина Е.Г.",
    "Магнитская Н.А.", "Сбитнев В.В.", "Тё С.А.", "Чамеева Т.В.", "Не указано",
]


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


def test_canonicalize_skips_multiselect():
    # multiselect values are lists joined by normalize_extracted — never canonicalized.
    out = canonicalize_extracted(
        {"m": ["x", "y"]}, {"m": ["x", "y", "z"]}, {"m": "multiselect"}
    )
    assert out["m"] == ["x", "y"]


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


_ACCESS_OPTIONS = ["трансвагинальный", "трансабдоминальный", "трансректальный"]


def test_normalize_extracted_multiselect_unknown_item_appends_last():
    out = normalize_extracted(
        {"a": ["неизвестный", "трансабдоминальный", "трансвагинальный"]},
        {"a": "multiselect"},
        {},
        enums={"a": _ACCESS_OPTIONS},
    )
    assert out["a"] == "трансвагинальный, трансабдоминальный, неизвестный"


def test_normalize_extracted_prefix_empty_applies_default():
    out = normalize_extracted(
        {"a": {"prefix": "", "detail": ""}}, {"a": "prefix"}, {"a": "не расширен"}
    )
    assert out["a"] == "не расширен"


# ── render_template ────────────────────────────────────────────────────────


def test_render_template_missing_key():
    template = "# {{name}}\n\n{{missing_key}}"
    data = {"name": "Bob"}
    result = render_template(template, data, "doc-1", "Title")
    assert "# Bob" in result
    assert result.count("") or "{{missing_key}}" not in result


# ── render_title_template ──────────────────────────────────────────────────


def test_render_title_template_rnd_different_each_call():
    r1 = render_title_template("{rnd:16}", "Doc")
    r2 = render_title_template("{rnd:16}", "Doc")
    assert r1 != r2


def test_render_title_template_unknown_var_passthrough():
    result = render_title_template("{unknown}", "Doc")
    assert result == "{unknown}"


def test_render_title_template_bad_tz_falls_back_to_utc():
    from datetime import datetime
    from datetime import timezone as _tz
    expected_hour = datetime.now(_tz.utc).strftime("%H")
    result = render_title_template("{HH}", "Doc", tz_name="Not/AReal_Zone")
    assert result == expected_hour


# ── parse_pipeline_config_from_doc ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_pipeline_config_from_doc_malformed_yaml_raises():
    mock_doc = {"content": "```yaml\n: : not a dict\n```"}
    with patch("pipeline.core.config.fetch_one", return_value=mock_doc):
        with pytest.raises((ValueError, Exception)):
            await parse_pipeline_config_from_doc("cfg-bad")


# ── fetch_child_rows ────────────────────────────────────────────────────────


def _make_row(title, content):
    return {"title": title, "content": content}


# ── resolve_variables_doc / resolve_instructions_doc ────────────────────────


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
async def test_call_llm_structured_error_carries_provider_body():
    body = '{"error":{"message":"This response_format type is unavailable now"}}'
    error_resp = httpx.Response(400, text=body, request=httpx.Request("POST", "http://llm/chat/completions"))

    async def mock_post(url, json, headers):
        return error_resp

    mock_client = AsyncMock()
    mock_client.post = mock_post
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    with patch("pipeline.extractor.utils.httpx.AsyncClient", return_value=mock_cm):
        with pytest.raises(RuntimeError) as exc_info:
            await call_llm_structured(
                user_prompt="p", json_schema={"type": "object"}, model="deepseek/flash",
            )
    message = str(exc_info.value)
    assert "400" in message
    assert "deepseek/flash" in message
    assert "response_format type is unavailable now" in message


# ── build_json_schema with enums ────────────────────────────────────────────


def test_build_json_schema_with_enum():
    variables = {"role": "The user role"}
    enums = {"role": ["admin", "user"]}
    schema = build_json_schema(variables, enums=enums)
    assert schema["properties"]["role"]["enum"] == ["admin", "user"]
    assert schema["properties"]["role"]["type"] == "string"


# ── resolve_variables_sections: refine section ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_calculate_output_dropped(caplog):

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
            variables, types, calculations, enums, kinds, defaults, separators,
            duplicates, refine,
        ) = await resolve_variables_sections("cfg-1")

    assert refine == [{"fields": ["width"], "prompt": None}]
    assert "area" in caplog.text


@pytest.mark.asyncio
async def test_resolve_variables_sections_refine_pass_dict_keeps_prompt():

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
            variables, types, calculations, enums, kinds, defaults, separators,
            duplicates, refine,
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


# ── ComputeNode ─────────────────────────────────────────────────────────────


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


# ── apply_dictionary: config-driven output typography (CIR C5, 2026-07-27) ──


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


# ── E4: numeral folding + numeral-subset guard in the canonicalizer (CIR, 2026-07-27) ──


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
