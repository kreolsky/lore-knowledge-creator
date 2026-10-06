"""Utility functions for the extractor pipeline.

call_llm_structured — non-streaming LLM call with Structured Outputs.
render_title_template — render document title from a template with date/random variables.
build_json_schema — build JSON Schema from variables dict.
canonicalize_extracted — snap free-extracted categorical strings to canonical options.
render_template — replace {{key}} placeholders in markdown template.
render_prompt — render user prompt template with placeholders.
"""
import difflib
import logging
import random
import re
import string
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import settings

from config import CHAT_LLM_TIMEOUT_S
from pipeline.core.constants import ALLOWED_VARIABLE_TYPES

logger = logging.getLogger(__name__)


async def call_llm_structured(
    user_prompt: str,
    json_schema: dict,
    model: str | None = None,
) -> dict:
    """Call LLM with Structured Outputs (json_schema response_format).

    Returns parsed JSON dict from the LLM response.
    Raises RuntimeError on failure.
    """
    api_url = await settings.get("AI_API_URL")
    api_key = await settings.get("AI_API_KEY")
    effective_model = model or await settings.get("CHAT_MODEL")

    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": user_prompt}],
        # WHY: temperature=0 (greedy) — extraction must be deterministic. Sampling
        # (server default ~0.7) made identical input yield different dates/day_cycle
        # and let the model ignore the "" enum escape; reproducibility is also a
        # hard requirement for the gold-eval program (Steps 2-4).
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "extraction",
                "strict": True,
                "schema": json_schema,
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logger.debug("[call_llm_structured] model=%s (override=%s) prompt_length=%d", effective_model, model, len(user_prompt))
    logger.debug("[call_llm_structured] json_schema=%s", json_schema)

    async with httpx.AsyncClient(timeout=CHAT_LLM_TIMEOUT_S) as client:
        resp = await client.post(
            f"{api_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    import json
    content = data["choices"][0]["message"]["content"]
    logger.debug("[call_llm_structured] LLM raw response: %s", content)
    parsed = json.loads(content)
    logger.info("[call_llm_structured] parsed keys=%s", list(parsed.keys()))
    return parsed


def _categorical_schema_prop(kind: str, options: list[str], description: str) -> dict:
    """JSON-Schema property for a categorical field. See SYSTEM: extractor.

    - enum        → {"type":"string"}                         (free transcription)
    - prefix      → {"type":"string"}                         (free transcription)
    - multiselect → array of enum items                       (several from the list)

    ARCH: enum/prefix are not grammar-constrained.
    The local model (local/orange/chat) is measurably MORE accurate transcribing the
    spoken phrase freely than picking from a forced `enum` grammar, which coerced wrong
    options (Доктор «Збитник» → wrong roster name; localization_uterus wrong axis). The
    `options:` list now feeds the DETERMINISTIC downstream canonicalizer
    (canonicalize_extracted) instead of the schema. multiselect keeps its enum grammar
    (out of Variant 4 scope).

    INVARIANT: enum/prefix stay emptyable. Why: a plain string naturally permits "" so
    the model can signal "not stated" (Rule 2) and a field's `default:` still fires via
    normalize_extracted — the empty escape that a forced-choice enum needed is now free.
    """
    # multiselect keeps its enum grammar; absence is [] (no escape value needed).
    if kind == "multiselect":
        return {
            "type": "array",
            "items": {"type": "string", "enum": options},
            "description": description,
        }
    # enum / prefix: free-string transcription, canonicalized deterministically later.
    return {"type": "string", "description": description}


def build_json_schema(
    variables: dict,
    *,
    types: dict[str, str] | None = None,
    enums: dict[str, list[str]] | None = None,
    kinds: dict[str, str] | None = None,
) -> dict:
    """Build a JSON Schema from a variables dict for Structured Outputs.

    Types are taken from the optional ``types`` dict. Defaults to "string" for
    any variable not present in ``types``.

    ``kinds`` maps a variable name to its categorical kind (enum/multiselect/prefix);
    when present, the property schema is emitted per-kind (see _categorical_schema_prop)
    with ``options`` taken from ``enums[name]``.

    ``enums`` without a ``kinds`` entry keeps the legacy behavior: a plain-string
    property gains a single ``enum`` constraint (backward compat for old callers).

    variables dict maps names to instruction descriptions, e.g.
    {"character_name": "The name of the character"}.
    """
    effective_types = types or {}
    effective_enums = enums or {}
    effective_kinds = kinds or {}
    properties = {}
    for name in variables:
        description = str(variables[name])
        if name in effective_kinds:
            properties[name] = _categorical_schema_prop(
                effective_kinds[name], effective_enums.get(name, []), description
            )
            continue
        json_type = effective_types.get(name, "string")
        if json_type not in ALLOWED_VARIABLE_TYPES:
            raise ValueError(
                f"Variable '{name}': unknown type '{json_type}'. Allowed: {ALLOWED_VARIABLE_TYPES}"
            )
        # INVARIANT: number/integer fields widen to [type, "null"] so the model can  Why: a bare numeric type can't represent 'absent', so default never fires and the model fabricates a value; widening to [type,"null"] lets the model signal null instead.
        # signal absence — a bare numeric type is un-emptyable, so `default:` could
        # never fire and the model invents a value (postmenopause → 7 when 'менопауза'
        # was never spoken). Why null, NOT a "string" union: a string union let the
        # model dump raw non-numeric text ('day_cycle_raw' → 'шестой'), crashing a
        # `calculate:` input on float(). null permits ABSENCE without permitting junk.
        # normalize_extracted maps None → default BEFORE ComputeNode (see INVARIANT in
        # nodes.py), so compute always sees a number.
        schema_type: str | list[str] = (
            [json_type, "null"] if json_type in ("number", "integer") else json_type
        )
        prop = {"type": schema_type, "description": description}
        if name in effective_enums:
            prop["enum"] = effective_enums[name]
        properties[name] = prop

    required = list(variables.keys())

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _join_items(items: list, separator: str = ", ") -> str:
    """Render a list by joining every gap with separator (default ", ").

    A config that wants «X и Y и Z» declares `separator: " и "` on the variable
    (config.py::_normalize_variable_entry), which lands here verbatim.
    """
    return separator.join(str(i) for i in items)


# WHY: 0.55 (not 0.6) — a surname ASR slip like «Збитник»→«Сбитнев» scores 0.57 on the
# token-aware ratio; 0.6 would drop it back to the free string. Tuned against cir/.
_CANON_FUZZY_THRESHOLD = 0.55


def _fuzzy_score(nv: str, no: str) -> float:
    """Full-string ratio, lifted by the option's HEAD-token ratio for single-word values.

    A lone surname («Збитник») vs a 'Surname Initials' option scores low on the full
    string (diluted by the initials) but high against the option's first token (the
    surname) — so for a one-word value we also take the head-token ratio. Restricted to
    the FIRST token deliberately: matching ANY option token would let a bare partial like
    'anteflexio' hit the 'anteflexio' token buried in a multi-axis option and fabricate
    the unspoken deviation axis (see _canonicalize_one's coverage guard for the same
    class). Multi-word values keep the pure full-string ratio.
    """
    full = difflib.SequenceMatcher(None, nv, no).ratio()
    v_tokens, o_tokens = nv.split(), no.split()
    if len(v_tokens) != 1 or not o_tokens:
        return full
    head = difflib.SequenceMatcher(None, v_tokens[0], o_tokens[0]).ratio()
    return max(full, head)


def _canon_norm(s: str) -> str:
    """Normalize for canonical comparison: lower, ё=е, drop punct, collapse ws, fold numerals.

    The first four steps mirror cir/compare.py::_norm so the canonicalizer and the
    benchmark comparator agree on what "equal" means. Numeral folding is canonicalizer-only
    (see _fold_numerals) — the comparator has its own, different numeral handling.
    """
    s = str(s).strip().lower().replace("ё", "е")
    s = re.sub(r"[^\w\s.\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _fold_numerals(s)


# Numeral folding, ru. INVARIANT(corruption): a numeral difference must survive
# normalization as a DISCRIMINATOR, not be averaged away by difflib. Why: difflib scores
# «первой фазе менструального цикла» and «второй фазе менструального цикла» identically
# (0.881) against option «1 фазе менструального цикла», so the winner is iteration order
# — always the first option — which silently swaps the cycle phase in a medical
# conclusion. Folding to a word STEM (not a digit) is what carries the discriminator:
# measured margin between the right and the wrong option is 0.000 as-is, 0.037 folding to
# digits, 0.101 folding to stems. Applied to BOTH sides, so it can only merge forms,
# never distort meaning; an unfolded numeral degrades to today's behaviour.
#
# WHY explicit ordinal endings and not `перв\w*`: a bare stem+`\w*` swallows ordinary
# medical vocabulary that merely starts the same way — «первичный» (primary), «вторичный»
# (secondary), «пятно» (a spot). Folding those to a numeral stem would let two unrelated
# words merge, which is the exact corruption this guard exists to prevent.
_ORD_END = r"(?:ый|ой|ая|ое|ые|ий|ья|ье|ьи|ому|ого|ым|ыми|ую|ом|ей|ему|его|им|ими|ых|ью)"
_NUMERAL_STEMS: list[tuple[str, str]] = [
    ("перв", rf"перв{_ORD_END}|один|одна|одно|одного|одному|одним|одну"),
    ("втор", rf"втор{_ORD_END}|два|две|двух|двум|двумя"),
    ("трет", rf"трет{_ORD_END}|три|трех|трем|тремя"),
    ("четверт", rf"четверт{_ORD_END}|четыре|четырех|четырем|четырьмя"),
    ("пят", rf"пят{_ORD_END}|пять|пяти|пятью"),
    ("шест", rf"шест{_ORD_END}|шесть|шести|шестью"),
    ("седьм", rf"седьм{_ORD_END}|семь|семи|семью"),
    ("восьм", rf"восьм{_ORD_END}|восемь|восьми|восемью"),
    ("девят", rf"девят{_ORD_END}|девять|девяти|девятью"),
    ("десят", rf"десят{_ORD_END}|десять|десяти|десятью"),
    ("одиннадцат", rf"одиннадцат{_ORD_END}|одиннадцать|одиннадцати"),
    ("двенадцат", rf"двенадцат{_ORD_END}|двенадцать|двенадцати"),
]
_NUMERAL_WORD_RES = [
    (re.compile(rf"\b(?:{alts})\b"), stem) for stem, alts in _NUMERAL_STEMS
]
_DIGIT_STEMS = {str(i + 1): stem for i, (stem, _) in enumerate(_NUMERAL_STEMS)}
# WHY the guards: only STANDALONE ordinals/cardinals fold. A digit that introduces a
# measurement ('до 7 мм'), sits inside a dimension ('11х4х7мм' — one token after punct
# stripping — or the spaced '22 х 12 х 9 мм') or leads a longer number is data, not a
# numeral word; spelling those out would fabricate numerals the numeral guard then
# compares. The dimension separator is checked on BOTH sides: in '22 х 12 х 9 мм' the
# outer numbers are already safe (22 is out of range, 9 precedes a unit) but the MIDDLE
# one is reachable from neither end. Scope: ordinals/cardinals only; years and
# scale codes are left alone by the 1–12 range.
_DIGIT_RE = re.compile(
    r"(?<![хx×])(?<![хx×]\s)\b(1[0-2]|[1-9])\b(?!\s*(?:мм|см|мл|ед|шт|%|\d|[хx×]))"
)


def _fold_numerals(s: str) -> str:
    """Collapse standalone numerals 1–12 (digits and word forms) to a word stem."""
    for rx, stem in _NUMERAL_WORD_RES:
        s = rx.sub(stem, s)
    return _DIGIT_RE.sub(lambda m: _DIGIT_STEMS[m.group(1)], s)


_NUMERAL_STEM_RE = re.compile(
    r"\b(?:" + "|".join(stem for stem, _ in _NUMERAL_STEMS) + r")\b"
)


def _numerals(s: str) -> set[str]:
    """Set of folded numeral stems carried by a string.

    A SET, not a multiset: an option may legitimately restate one numeral in two
    notations — «2 (второй) фазе менструального цикла» folds to 'втор втор' — while the
    value from speech carries it once. Counting occurrences filtered exactly those
    options and broke canonicalization of the field the guard was written for.
    """
    return set(_NUMERAL_STEM_RE.findall(_canon_norm(s)))


# WHY `не\w*` and not `\bне\b`: Russian glues the negation onto the word
# ('нетипичном', 'неправильная', 'нечёткие', 'не деформированы' — both forms occur in
# the same option lists). A word-boundary-only match misses exactly the dangerous case:
# 'в нетипичном месте' vs 'в типичном месте' is ONE token apart, not two.
_NEGATION_RE = re.compile(r"\bне\w*|\bбез\b")


def _negations(s: str) -> int:
    """How many negation particles a normalized string carries."""
    return len(_NEGATION_RE.findall(_canon_norm(s)))


def _canonicalize_one(
    value: str, options: list[str], threshold: float = _CANON_FUZZY_THRESHOLD
) -> str | None:
    """Snap a free extracted string to a canonical option, or None to keep it free.

    Deterministic, stdlib only (difflib). Escalates: normalized-exact → containment
    (most specific / longest option wins, guarded by min length) → best fuzzy ratio
    ≥ threshold. No confident match → None (caller keeps the free string).
    """
    nv = _canon_norm(value)
    if not nv:
        return None
    # INVARIANT(corruption): never snap across a negation difference. Why: 'в
    # нетипичном месте' vs 'в типичном месте' differ by 2 chars and score ~0.96 —
    # above EVERY threshold — yet they are opposite findings; the same holds for
    # 'чёткие, деформированы' → 'чёткие, не деформированы'. Measured on the CIR
    # benchmark 2026-07-27; tuning the threshold provably cannot separate the two.
    # INVARIANT(corruption): never snap onto an option carrying a numeral the value
    # never spoke. Why: «2 фазе» and «O-RADS 3» are one token from their siblings and
    # score above every threshold, yet a swapped cycle phase or scale code is a
    # different clinical finding — the same class as the negation guard above.
    # DIRECTIONAL, not symmetric (option ⊆ value): _canonicalize_prefix routes the head
    # through here and a prefix tail legitimately carries measurements the option never
    # mentions ('не расширен, полип 11х4х7мм' must still snap onto 'не расширен').
    v_neg = _negations(value)
    v_nums = _numerals(value)
    norm_opts = [
        (opt, _canon_norm(opt))
        for opt in options
        if _negations(opt) == v_neg and not (_numerals(opt) - v_nums)
    ]
    # 1. normalized-exact
    for opt, no in norm_opts:
        if no and no == nv:
            return opt
    # 2a. option ⊂ value — ASR added noise around a clean full option. Safe to snap;
    #     pick the longest matching option ('правильная' ⊂ 'неправильная' → the longer).
    superset = [(opt, no) for opt, no in norm_opts if len(no) >= 4 and no in nv]
    if superset:
        return max(superset, key=lambda x: len(x[1]))[0]
    # 2b. value ⊂ option — the option appends tokens the value never spoke. Accept ONLY
    #     when the value covers most (≥60%) of the option; otherwise the "extra" is a
    #     meaning-bearing unspoken axis (bare 'anteflexio' ⊂ '…anteflexio, отклонено
    #     вправо') and snapping would FABRICATE it — keep the free string instead.
    subset = [
        (opt, no)
        for opt, no in norm_opts
        if len(nv) >= 4 and nv in no and len(nv) / len(no) >= 0.6
    ]
    if subset:
        return min(subset, key=lambda x: len(x[1]))[0]
    # 3. best fuzzy ratio ≥ threshold
    best, best_score = None, 0.0
    for opt, no in norm_opts:
        if not no:
            continue
        score = _fuzzy_score(nv, no)
        if score > best_score:
            best, best_score = opt, score
    if best_score >= threshold:
        return best
    # 4. no confident match → keep the free string
    return None


def canonicalize_extracted(
    extracted: dict,
    enums: dict[str, list[str]] | None = None,
    kinds: dict[str, str] | None = None,
    threshold: float = _CANON_FUZZY_THRESHOLD,
) -> dict:
    """Map free-extracted categorical strings to their canonical option (Variant 4).

    Runs AFTER ExtractionNode, BEFORE normalize_extracted/ComputeNode. For every field
    with configured ``options`` (enum/prefix), the free transcription is snapped to the
    closest option deterministically — no LLM (see _canonicalize_one). multiselect is
    skipped (its list shape is joined by normalize_extracted). A non-confident match
    keeps the free string (prod-like accuracy); empty passes through to default:.
    """
    effective_enums = enums or {}
    effective_kinds = kinds or {}
    result = dict(extracted)
    for name, options in effective_enums.items():
        if effective_kinds.get(name) == "multiselect":
            continue
        value = result.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        if effective_kinds.get(name) == "prefix":
            canon = _canonicalize_prefix(value, options, threshold)
        else:
            canon = _canonicalize_one(value, options, threshold)
        if canon is not None:
            result[name] = canon
    return result


def _canonicalize_prefix(
    value: str, options: list[str], threshold: float = _CANON_FUZZY_THRESHOLD
) -> str | None:
    """Canonicalize the HEAD of a prefix field, preserving the detail tail.

    INVARIANT(data-loss): a prefix value is "option + detail" — canonicalization
    replaces the head only, never the whole string. Why: plain _canonicalize_one
    matches the option as a SUBSTRING of the value and returns the bare option, so
    'не расширен, в просвете полип 11х4х7мм' collapsed to 'не расширен' and deleted a
    finding the field's own config description demands ('расширение И всё содержимое
    просвета'). Measured on CIR ref 392788, 2026-07-27.
    """
    canon = _canonicalize_one(value, options, threshold)
    if canon is None:
        return None
    opt_tokens = _canon_norm(canon).split()
    raw_tokens = value.split()
    norm_tokens = [_canon_norm(t) for t in raw_tokens]
    n = len(opt_tokens)
    # Locate the option as a literal token run; a fuzzy-only match has no tail to keep.
    for i in range(len(raw_tokens) - n + 1):
        if norm_tokens[i:i + n] == opt_tokens:
            tail = " ".join(raw_tokens[:i] + raw_tokens[i + n:]).strip(" ,;:.-")
            return f"{canon}, {tail}" if tail else canon
    return canon


def apply_dictionary(data: dict, replacements: dict[str, str] | None) -> dict:
    """Apply the config `dictionary` replacement table to every string value.

    Whole-word, case-insensitive; the replacement is written as configured. Non-string
    values (numbers, lists) pass through untouched.

    ARCH: runs LAST — after ComputeNode — so it never feeds a `calculate:` input.
    Why: folding 'восемь миллиметров' → '8 мм' before compute would change what the
    formulas parse; typography is a rendering concern, not an extraction one.
    """
    if not replacements:
        return data
    # Longest key first: 'кесарево сечение' must win over a shorter 'кесарево'.
    pairs = sorted(replacements.items(), key=lambda kv: -len(kv[0]))
    compiled = [
        (re.compile(rf"(?<!\w){re.escape(src)}(?!\w)", re.IGNORECASE), dst)
        for src, dst in pairs
    ]
    result = dict(data)
    for name, value in result.items():
        if not isinstance(value, str) or not value:
            continue
        for pattern, dst in compiled:
            # WHY: the replacement is written EXACTLY as configured — matching is
            # case-insensitive, the output is not re-cased. Why: the clinic controls the
            # printed form through the table; carrying the source's capitalization over
            # would make the same key print two ways depending on the model's output.
            value = pattern.sub(lambda _m, dst=dst: dst, value)
        result[name] = value
    return result


def _is_empty(value: object) -> bool:
    """True when an extracted value counts as absent for ``default:`` substitution."""
    return value is None or value == "" or value == []


def _sort_by_options(items: list, options: list[str]) -> list:
    """Order selected multiselect items by their index in the configured ``options``.

    Items absent from ``options`` sort last, keeping their relative order (the grammar
    constrains multiselect to the enum, so this is a safety net for hand-built dicts).
    """
    index = {opt: i for i, opt in enumerate(options)}
    return sorted(items, key=lambda item: index.get(item, len(index)))


def normalize_extracted(
    extracted: dict,
    kinds: dict[str, str] | None = None,
    defaults: dict[str, str] | None = None,
    enums: dict[str, list[str]] | None = None,
    separators: dict[str, str] | None = None,
) -> dict:
    """Collapse categorical extraction shapes to display strings and apply defaults.

    Runs AFTER extraction, BEFORE Compute/Render (which only ``str()`` values):
    - multiselect list  → separator-joined string (see _join_items), ordered by
      ``enums[name]`` when supplied; the separator comes from ``separators[name]``
      (the variable's ``separator:`` key), default ", ".
    - prefix object      → "prefix detail".strip().
    - any field whose collapsed value is empty AND has a ``default:`` → the default.

    Non-categorical keys pass through untouched. See SYSTEM: extractor.
    """
    effective_kinds = kinds or {}
    effective_defaults = defaults or {}
    effective_enums = enums or {}
    effective_separators = separators or {}
    result = dict(extracted)

    for name, value in list(result.items()):
        kind = effective_kinds.get(name)
        if kind == "multiselect":
            items = value if isinstance(value, list) else []
            # WHY: a multiselect renders in CONFIG option order, not the order the
            # model emitted. Why: without it both «трансабдоминальный и трансвагинальный»
            # and its reverse are reachable for the same finding; the config used to
            # absorb that by enumerating both orderings of every pair; this sort is
            # what lets the config carry each pair exactly once.
            options = effective_enums.get(name)
            if options:
                items = _sort_by_options(items, options)
            result[name] = _join_items(items, effective_separators.get(name, ", "))
        elif kind == "prefix" and isinstance(value, dict):
            prefix = value.get("prefix", "") or ""
            detail = value.get("detail", "") or ""
            result[name] = f"{prefix} {detail}".strip()

    for name, default in effective_defaults.items():
        if _is_empty(result.get(name)):
            result[name] = default

    return result


def render_template(
    template: str,
    data: dict,
    source_doc_id: str,
    source_doc_title: str,
    reference_id: str = "",
    reference_title: str = "",
) -> str:
    """Replace {{key}} placeholders in template with extracted data values.

    System variables available in the template (in addition to extracted fields):
        {{doc_id}}    — source document id
        {{ref_id}}    — reference id that was processed
        {{doc_title}} — source document title
        {{ref_title}} — reference title (the processed reference's display name)
        {{yyyy}}      — 4-digit year (UTC)
        {{mm}}        — 2-digit month (UTC)
        {{dd}}        — 2-digit day (UTC)
        {{HH}}        — 2-digit hour, 24h (UTC)
        {{MM}}        — 2-digit minute (UTC)
        {{SS}}        — 2-digit second (UTC)
    """
    now = datetime.now(timezone.utc)
    merged = {
        "doc_id": source_doc_id,
        "ref_id": reference_id,
        "doc_title": source_doc_title,
        "ref_title": reference_title,
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
        "HH": now.strftime("%H"),
        "MM": now.strftime("%M"),
        "SS": now.strftime("%S"),
        **data,
    }
    template_keys = set(re.findall(r"\{\{(\w+)\}\}", template))
    merged_keys = set(merged.keys())
    missing_in_merged = template_keys - merged_keys
    extra_in_merged = set(data.keys()) - template_keys
    if missing_in_merged:
        logger.warning("[render_template] template keys NOT in data (will be empty): %s", missing_in_merged)
    if extra_in_merged:
        logger.warning("[render_template] extracted data keys NOT in template: %s", extra_in_merged)
    logger.info("[render_template] template_keys=%s data_keys=%s", template_keys, merged_keys)

    def replacer(match):
        key = match.group(1).strip()
        value = str(merged.get(key, ""))
        logger.debug("[render_template] {{%s}} -> '%s'", key, value[:100])
        return value

    rendered = re.sub(r"\{\{(\w+)\}\}", replacer, template)
    return rendered


_RND_CHARS = string.ascii_letters + string.digits
_RND_PATTERN = re.compile(r"rnd:(\d+)")


def _build_title_vars(now: datetime, source_title: str, reference_title: str = "") -> dict[str, Callable]:
    """Build resolver callables for title template variables."""
    return {
        "yyyy": lambda: now.strftime("%Y"),
        "mm": lambda: now.strftime("%m"),
        "dd": lambda: now.strftime("%d"),
        "HH": lambda: now.strftime("%H"),
        "MM": lambda: now.strftime("%M"),
        "title": lambda: source_title,
        "doc_title": lambda: source_title,
        "ref_title": lambda: reference_title,
    }


def _resolve_tz(tz_name: str | None) -> timezone | ZoneInfo:
    """Return a tzinfo for the given IANA name, falling back to UTC on bad input."""
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("[render_title_template] unknown tz_name=%r, falling back to UTC", tz_name)
        return timezone.utc


def render_title_template(
    template: str | None,
    source_title: str,
    default_template: str | None = None,
    *,
    reference_title: str = "",
    tz_name: str | None = None,
) -> str:
    """Render a document title from a template with built-in variables.

    Supported variables:
        {yyyy}      — 4-digit year
        {mm}        — 2-digit month
        {dd}        — 2-digit day
        {HH}        — 2-digit hour (24h, in tz_name if provided)
        {MM}        — 2-digit minute
        {title}     — source document title (alias: {doc_title})
        {ref_title} — processed reference title
        {rnd:N}     — N random chars from [a-zA-Z0-9]
    """
    from pipeline.core.constants import DEFAULT_TITLE_TEMPLATE
    fallback = default_template or DEFAULT_TITLE_TEMPLATE
    tmpl = template.strip() if template and template.strip() else fallback
    now = datetime.now(_resolve_tz(tz_name))
    vars_map = _build_title_vars(now, source_title, reference_title)

    def replacer(match: re.Match) -> str:
        var = match.group(1)
        resolver = vars_map.get(var)
        if resolver:
            return resolver()
        rnd_match = _RND_PATTERN.match(var)
        if rnd_match:
            return "".join(random.choices(_RND_CHARS, k=int(rnd_match.group(1))))
        return match.group(0)

    result = re.sub(r"\{([^}]+)\}", replacer, tmpl)
    logger.info("[render_title_template] template='%s' source_title='%s' tz=%s result='%s'", tmpl, source_title, tz_name, result)
    return result


def render_prompt(
    prompt_template: str,
    variables: dict,
    transcription_text: str,
    instructions: str,
    child_docs: dict,
) -> str:
    """Render the user prompt template by replacing placeholders.

    {transcription} -> transcription text (built-in)
    {variables} / {fields} -> formatted variable list
    {instructions} / {user_instructions} -> instructions text
    Other {placeholder} -> child doc content (resolved by title)
    """
    from pipeline.core.constants import BUILTIN_PROMPT_VARS

    var_lines = "\n".join(f"- `{k}`: {v}" for k, v in variables.items())

    replacements = {
        "transcription": transcription_text,
        "variables": var_lines,
        "fields": var_lines,
        "instructions": instructions,
        "user_instructions": instructions,
    }

    for placeholder, content in child_docs.items():
        if placeholder not in replacements and placeholder not in BUILTIN_PROMPT_VARS:
            replacements[placeholder] = content

    def replacer(match):
        key = match.group(1)
        return replacements.get(key, match.group(0))

    rendered = re.sub(r"\{(\w+)\}", replacer, prompt_template)
    return rendered
