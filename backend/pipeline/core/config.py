"""Config resolution — parse YAML from config-documents, resolve child docs and templates.

Config resolution order:
1. YAML from config-document first code block
2. Defaults from core/constants.py (retries, prompt)
3. title_template from agent_config DB record overrides defaults
"""
# SYSTEM: pipeline-config — config resolution from YAML documents + child docs
import logging
import re

import yaml

from db import fetch_one, get_db
from pipeline.core.constants import (
    ALLOWED_VARIABLE_TYPES,
    CATEGORICAL_VARIABLE_TYPES,
    DEFAULT_PROMPT,
)

logger = logging.getLogger(__name__)


class _DupCollectingLoader(yaml.SafeLoader):
    """SafeLoader that COLLECTS duplicate mapping keys instead of silently last-wins.

    yaml.safe_load drops earlier definitions of a repeated key with no trace; this
    loader keeps identical last-wins output but records every duplicated key on the
    instance (``_duplicate_keys``) so the caller can surface a warning. See SYSTEM:
    extractor — duplicate variable definitions must become visible, not block.
    """


def _construct_mapping_collect_dups(loader, node, deep=False):
    # WHY: SafeLoader resolves merge keys (`<<: *anchor`) in flatten_mapping, which an
    # overriding constructor must call itself — without it every `<<:` raises
    # "could not determine a constructor for the tag 'tag:yaml.org,2002:merge'", and
    # a shared defaults block for a group of variables cannot be declared once.
    loader.flatten_mapping(node)
    mapping = {}
    dups = loader.__dict__.setdefault("_duplicate_keys", [])
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping and key not in dups:
            dups.append(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DupCollectingLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_collect_dups
)


def _safe_load_collect_dups(yaml_str: str) -> tuple[object, list[str]]:
    """yaml.safe_load equivalent that also returns the list of duplicated keys."""
    loader = _DupCollectingLoader(yaml_str)
    try:
        data = loader.get_single_data()
    finally:
        loader.dispose()
    return data, getattr(loader, "_duplicate_keys", [])


def _normalize_yaml_indentation(yaml_str: str) -> str:
    """Replace leading tabs with spaces (1 tab = 4 spaces).

    Only transforms indentation — tabs inside quoted strings are preserved.
    """
    out = []
    for line in yaml_str.split("\n"):
        stripped = line.lstrip()
        if stripped == line:
            out.append(line)
        else:
            leading = line[: len(line) - len(stripped)]
            out.append(leading.replace("\t", "    ") + stripped)
    return "\n".join(out)


def _build_yaml_error_snippet(yaml_str: str, mark: object, context_lines: int = 3) -> str:
    """Extract lines around a YAML error mark with line numbers prefixed."""
    lines = yaml_str.split("\n")
    line_no = mark.line if hasattr(mark, "line") else 0
    start = max(0, line_no - context_lines)
    end = min(len(lines), line_no + context_lines + 1)
    snippet_lines = []
    for i in range(start, end):
        prefix = ">>>" if i == line_no else "   "
        snippet_lines.append(f"{prefix} {i + 1:3d} | {lines[i]}")
    return "\n".join(snippet_lines)


def _parse_yaml_from_code_block(
    content: str, *, doc_label: str = "YAML", dup_sink: list[str] | None = None
) -> dict:
    """Extract YAML from the first fenced code block and parse it.

    Returns a dict. If no code block found, tries to parse the whole content as YAML.
    Handles YAML content that itself contains backtick sequences by matching the
    exact opening-fence length for the closing fence (per Markdown spec).
    On YAML syntax errors, raises ValueError with line number and context snippet.

    When ``dup_sink`` is a list, any duplicated mapping keys found while parsing are
    appended to it (last-wins output is unchanged) — see _DupCollectingLoader.
    """
    open_match = re.search(r"^[ \t]*(`{3,})(?:ya?ml)?[ \t]*\n", content, re.MULTILINE)
    if not open_match:
        logger.debug("[_parse_yaml] no fenced code block found, parsing whole content (len=%d)", len(content))
        normed = _normalize_yaml_indentation(content)
        try:
            parsed, dups = _safe_load_collect_dups(normed)
        except yaml.YAMLError as e:
            raise _enrich_yaml_error(e, normed, doc_label) from e
        if dup_sink is not None:
            dup_sink.extend(dups)
        if not isinstance(parsed, dict):
            raise ValueError(f"YAML config must be a dict, got {type(parsed).__name__}")
        return parsed

    fence = open_match.group(1)
    fence_len = len(fence)
    yaml_start = open_match.end()

    close_pattern = re.compile(rf"^[ \t]*`{{{fence_len}}}[ \t]*$", re.MULTILINE)
    close_match = close_pattern.search(content, yaml_start)
    yaml_end = close_match.start() if close_match else len(content)
    yaml_str = content[yaml_start:yaml_end].strip()

    logger.debug(
        "[_parse_yaml] fence_len=%d yaml_start=%d yaml_end=%d extracted_len=%d first100=%s",
        fence_len, yaml_start, yaml_end, len(yaml_str), yaml_str[:100],
    )

    yaml_str = _normalize_yaml_indentation(yaml_str)
    try:
        parsed, dups = _safe_load_collect_dups(yaml_str)
    except yaml.YAMLError as e:
        logger.error(
            "[_parse_yaml] YAML parse error in %s. yaml_str first300=%s",
            doc_label, yaml_str[:300],
        )
        raise _enrich_yaml_error(e, yaml_str, doc_label) from e
    if dup_sink is not None:
        dup_sink.extend(dups)
    if not isinstance(parsed, dict):
        raise ValueError(f"YAML config must be a dict, got {type(parsed).__name__}")

    if len(parsed) == 1:
        inner = next(iter(parsed.values()))
        if isinstance(inner, dict):
            logger.debug("[_parse_yaml] flattening single-key wrapper, inner keys=%s", list(inner.keys()))
            parsed = inner

    return parsed


def _enrich_yaml_error(error: yaml.YAMLError, yaml_str: str, doc_label: str) -> ValueError:
    """Convert a yaml.YAMLError into a ValueError with line/column and snippet."""
    parts = [f"YAML parse error in {doc_label}"]

    mark = getattr(error, "problem_mark", None)
    if mark:
        col = mark.column + 1 if hasattr(mark, "column") else "?"
        parts.append(f"at line {mark.line + 1}, column {col}")
        snippet = _build_yaml_error_snippet(yaml_str, mark)
        parts.append(f"```\n{snippet}\n```")

    problem = getattr(error, "problem", None)
    if problem:
        parts.append(problem)

    return ValueError("\n\n".join(parts))


def _extract_template_from_code_block(content: str) -> str:
    """Extract Markdown template from the first fenced code block.

    If no code block found, returns the content as-is.
    """
    match = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", content, re.DOTALL)
    return match.group(1).strip() if match else content.strip()


def _validate_options(name: str, options: object, *, key: str = "options") -> list[str]:
    """Validate a categorical field's option list (non-empty, all strings).

    ``key`` names the source knob in error messages ("options" for typed fields,
    "enum" for the legacy alias key).
    """
    if not isinstance(options, list) or len(options) == 0:
        raise ValueError(
            f"Variable '{name}': '{key}' must be a non-empty list of strings"
        )
    if not all(isinstance(v, str) for v in options):
        raise ValueError(f"Variable '{name}': all '{key}' values must be strings")
    return options


def _normalize_variable_entry(
    name: str, value: str | dict
) -> tuple[str, str, list[str] | None, str | None, str | None]:
    """Normalize a variable entry to (description, json_type, options, kind, default).

    - String values → plain string field (no options/kind/default).
    - Categorical fields declare ``type: enum|multiselect|prefix`` + ``options: [...]``;
      ``kind`` is that type and ``json_type`` is "string" (schema shape comes from kind).
    - The legacy ``enum:`` key is an alias for ``type: enum`` + ``options`` = that list,
      and is mutually exclusive with an explicit ``type:``.
    - Optional ``default:`` (any field) must be a string; applied when extraction is empty.
    Unknown dict keys are ignored.
    """
    if isinstance(value, str):
        return value, "string", None, None, None
    if not isinstance(value, dict):
        raise ValueError(
            f"Variable '{name}': expected string or dict, got {type(value).__name__}"
        )

    description = value.get("description", "")

    default = value.get("default")
    if default is not None and not isinstance(default, str):
        raise ValueError(f"Variable '{name}': 'default' must be a string")

    explicit_type = value.get("type")
    legacy_enum = value.get("enum")

    if legacy_enum is not None and explicit_type is not None:
        raise ValueError(
            f"Variable '{name}': legacy 'enum:' key and explicit 'type:' are mutually "
            f"exclusive — use one. Prefer 'type: enum' + 'options'."
        )

    # INVARIANT: legacy `enum:` normalizes to kind=enum + options. Why: the live config
    # relies on the bare `enum:` key; it must keep working after the type: migration.
    if legacy_enum is not None:
        options = _validate_options(name, legacy_enum, key="enum")
        return description, "string", options, "enum", default

    json_type = explicit_type or "string"

    if json_type in CATEGORICAL_VARIABLE_TYPES:
        options = _validate_options(name, value.get("options"))
        return description, "string", options, json_type, default

    if json_type not in ALLOWED_VARIABLE_TYPES:
        raise ValueError(
            f"Variable '{name}': unknown type '{json_type}'. "
            f"Allowed: {ALLOWED_VARIABLE_TYPES | CATEGORICAL_VARIABLE_TYPES}"
        )
    return description, json_type, None, None, default


def _extract_sections(parsed: dict) -> tuple[dict, dict, list]:
    """Extract variables, calculate, and refine sections from parsed YAML.

    Returns (variables_section, calculate_section, refine_entries). Handles both
    multi-section ({variables: {...}, calculate: {...}}) and single-section
    ({name: value}) backward-compatible YAML. The ``refine:`` section is a LIST
    of passes — a bare variable name (string) or a dict with ``fields:`` and an
    optional ``prompt:`` (entry shapes are validated in _resolve_refine_passes);
    a non-list shape is a config error and fails loud.
    """
    if "variables" in parsed:
        refine = parsed.get("refine", [])
        if not isinstance(refine, list):
            raise ValueError(
                f"The 'refine:' section must be a list of passes, "
                f"got {type(refine).__name__}"
            )
        return parsed["variables"], parsed.get("calculate", {}), refine
    return parsed, {}, []


async def _get_variables_raw(
    config_doc_id: str, *, rows: list[dict] | None = None, dup_sink: list[str] | None = None
) -> dict:
    """Find the variables child document and return parsed YAML dict.

    Returns the raw parsed dict (may contain variables/calculate sections).
    When ``dup_sink`` is a list, duplicated keys in the variables doc are appended to it.
    Raises ValueError if variables doc not found.
    """
    var_names = ["variables", "values"]

    if rows is None:
        rows = await fetch_child_rows(config_doc_id)

    for row in rows:
        title = (row.get("title") or "").strip().lower()
        content = row.get("content") or ""
        row_id = row.get("id", "?")
        if title in [n.lower() for n in var_names]:
            return _parse_yaml_from_code_block(
                content, doc_label=f"variables document {row_id}", dup_sink=dup_sink
            )

    raise ValueError(f"No 'variables' child found under config doc {config_doc_id}")


async def parse_pipeline_config_from_doc(config_doc_id: str) -> dict:
    """Load config document, extract YAML config from first code block.

    Returns a dict with keys ``pipeline`` and ``prompt``. Missing keys are filled
    with defaults from constants.py. Empty content yields defaults; malformed
    YAML raises ValueError so the caller's error-note path runs.
    """
    doc = await fetch_one("documents", config_doc_id)
    if not doc:
        raise ValueError(f"Config document {config_doc_id} not found")

    content = (doc.get("content") or "").strip()

    config: dict = {}
    if content:
        try:
            config = _parse_yaml_from_code_block(
                content, doc_label=f"config document {config_doc_id}"
            )
        except (ValueError, yaml.YAMLError):
            # WHY: malformed YAML must surface — no silent fallback to defaults.
            # The extract task's error path attaches an error note to the source
            # doc (`_create_error_note` in jobs/tasks/extract.py).
            logger.error(
                "[parse_pipeline_config_from_doc] Malformed YAML in config doc %s",
                config_doc_id,
            )
            raise

    return {
        "pipeline": config.get("pipeline", "extractor"),
        "prompt": config.get("prompt", DEFAULT_PROMPT),
    }


async def fetch_child_rows(config_doc_id: str) -> list[dict]:
    """Fetch all non-deleted child documents for a config doc (single DB query)."""
    db = await get_db()
    rows = await db.query(
        "SELECT * FROM documents WHERE parent_id = $pid AND deleted_at IS NONE",
        {"pid": config_doc_id},
    )
    return rows or []


async def resolve_child_docs(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> dict[str, str]:
    """Resolve ALL child documents to {title_lowercase: content} dict.

    Returns all child docs regardless of prompt placeholders.
    render_prompt handles deduplication — it skips placeholders already in its replacements.
    """
    if rows is None:
        rows = await fetch_child_rows(config_doc_id)

    resolved: dict[str, str] = {}
    for row in rows:
        title = (row.get("title") or "").strip().lower()
        content = row.get("content") or ""
        resolved[title] = content

    return resolved


async def resolve_template_doc(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> str:
    """Find the 'template' child document and extract template from its code block.

    Returns the template string.
    Raises ValueError if template doc not found.
    """
    if rows is None:
        rows = await fetch_child_rows(config_doc_id)

    for row in rows:
        title = (row.get("title") or "").strip().lower()
        if title == "template":
            content = row.get("content") or ""
            return _extract_template_from_code_block(content)

    raise ValueError(
        f"No 'template' child document found under config doc {config_doc_id}"
    )


async def resolve_variables_sections(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> tuple[
    dict[str, str], dict[str, str], dict[str, str],
    dict[str, list[str]], dict[str, str], dict[str, str], list[str],
    list[str],
]:
    """Parse variables YAML once.

    Returns (variables, types, calculations, enums, kinds, defaults, duplicates,
    refine_passes).
    - enums: {name: options list} for every categorical field (enum/multiselect/prefix).
    - kinds: {name: categorical type} — drives per-kind JSON schema and post-extract collapse.
    - defaults: {name: default string} applied when the extracted value is empty.
    - duplicates: variable names defined more than once (last-wins parse; warning only).
    - refine_passes: declared `refine:` list, one dict {fields, prompt} per pass,
      validated against `variables` and `calculations` (unknown names fail loud,
      calculate: outputs are dropped; a pass prompt without {transcription} raises).
    Single YAML parse. Raises ValueError if variables doc not found.
    """
    return await _resolve_variables_sections(config_doc_id, rows=rows)


def _normalize_refine_pass_entry(entry: object) -> tuple[list[str], str | None]:
    """Validate one `refine:` entry and return (names, prompt).

    A bare string is a single-field pass without its own prompt; a dict carries
    ``fields:`` (non-empty list of names) and an optional ``prompt:`` that
    replaces the first pass's template entirely. Everything else — unknown dict
    keys, an empty ``fields:``, a blank ``prompt:`` — fails loud.
    """
    if isinstance(entry, str):
        return [entry], None
    if not isinstance(entry, dict):
        raise ValueError(
            f"The 'refine:' entries must be variable names or pass dicts, "
            f"got {type(entry).__name__}. Fix the 'refine:' list in the "
            "variables document."
        )
    unknown_keys = set(entry) - {"fields", "prompt"}
    if unknown_keys:
        raise ValueError(
            f"A refine pass has unknown keys {sorted(unknown_keys)}; "
            "allowed: 'fields', 'prompt'. Fix the 'refine:' list in "
            "the variables document."
        )
    names = entry.get("fields")
    prompt = entry.get("prompt")
    if (
        not isinstance(names, list) or not names
        or not all(isinstance(n, str) for n in names)
    ):
        raise ValueError(
            "A refine pass 'fields:' must be a non-empty list of "
            "variable names. Fix the 'refine:' list in the variables "
            "document."
        )
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
        raise ValueError(
            "A refine pass 'prompt:' must be a non-empty string. Fix "
            "the 'refine:' list in the variables document."
        )
    if prompt is not None and "{transcription}" not in prompt:
        # WHY: the prompt is the only channel that carries the transcript to the
        # model, so a prompt without the placeholder extracts from nothing — and
        # that must fail loud at resolve time (same posture as a typo'd name),
        # never fall back to the default template.
        raise ValueError(
            "A refine pass 'prompt:' must contain the {transcription} "
            "placeholder — without it the pass extracts from nothing. "
            "Fix the 'refine:' list in the variables document."
        )
    return names, prompt


def _resolve_refine_passes(
    refine_section: list,
    variables: dict[str, str],
    calculations: dict[str, str],
) -> list[dict]:
    """Normalize the `refine:` list into passes: ``{fields, prompt}``.

    Entry shapes are validated in _normalize_refine_pass_entry; this loop
    validates the field names the way the bare list always was — unknown names
    fail loud, ``calculate:`` outputs are dropped with a warning (a pass reduced
    to nothing by those drops is skipped; the warning already fired).
    """
    passes: list[dict] = []
    for entry in refine_section:
        names, prompt = _normalize_refine_pass_entry(entry)
        pass_fields: list[str] = []
        for name in names:
            if name in calculations:
                logger.warning(
                    "[_resolve_variables_sections] refine field '%s' is a calculate: "
                    "output and is never extracted — dropping it",
                    name,
                )
                continue
            if name not in variables:
                raise ValueError(
                    f"Refine field '{name}' is not a declared variable. "
                    "Fix the 'refine:' list in the variables document."
                )
            pass_fields.append(name)
        if pass_fields:
            passes.append({"fields": pass_fields, "prompt": prompt})
    return passes


async def _resolve_variables_sections(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> tuple:
    """Internal: parse variables YAML once.

    Returns (variables, types, calculations, enums, kinds, defaults, duplicates,
    refine_passes).
    """
    duplicates: list[str] = []
    raw = await _get_variables_raw(config_doc_id, rows=rows, dup_sink=duplicates)
    vars_section, calc_section, refine_section = _extract_sections(raw)

    logger.debug(
        "[_resolve_variables_sections] vars_section keys=%s calc_section keys=%s",
        list(vars_section.keys()) if isinstance(vars_section, dict) else type(vars_section).__name__,
        list(calc_section.keys()) if isinstance(calc_section, dict) else type(calc_section).__name__,
    )

    variables: dict[str, str] = {}
    types: dict[str, str] = {}
    enums: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for name, value in vars_section.items():
        description, json_type, options, kind, default = _normalize_variable_entry(name, value)
        variables[name] = description
        types[name] = json_type
        if options is not None:
            enums[name] = options
        if kind is not None:
            kinds[name] = kind
        if default is not None:
            defaults[name] = default

    calculations: dict[str, str] = {}
    if isinstance(calc_section, dict):
        for name, value in calc_section.items():
            if isinstance(value, str) and value.startswith("="):
                calculations[name] = value
            else:
                logger.warning(
                    "[_resolve_variables_sections] calculated variable '%s' must start with '=': %s",
                    name, value,
                )

    refine_passes = _resolve_refine_passes(refine_section, variables, calculations)

    return variables, types, calculations, enums, kinds, defaults, duplicates, refine_passes


async def resolve_typography_doc(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> dict[str, str]:
    """Find the `typography` child doc and parse its `replacements:` table.

    ARCH: output typography (spelled-out units → symbols: 'миллиметров' → 'мм',
    'штук' → 'шт.') is a DETERMINISTIC post-step driven by config, not a prompt rule.
    Deliberately NOT the `dictionary` doc: that one is a PROMPT SLOT — the pipeline
    template injects it verbatim under "## Important information", so anything put
    there is read by the model and changes extraction. Measured 2026-07-27: filling
    `dictionary` with a replacement table shifted 8 unrelated fields.
    Why: the model rewrites units inconsistently and prompt-level bans on this class
    measurably do not hold (CIR 2026-07-27); a replacement table also lets the clinic
    change the printed form without a code change.

    Shape (yaml in a code block, same as the other config children):

        replacements:
          "миллиметров": "мм"
          "штук": "шт."

    Returns {} when the doc is absent, empty, or has no `replacements:` key.
    """
    if rows is None:
        rows = await fetch_child_rows(config_doc_id)

    for row in rows:
        if (row.get("title") or "").strip().lower() != "typography":
            continue
        content = (row.get("content") or "").strip()
        if not content:
            return {}
        try:
            parsed = _parse_yaml_from_code_block(content)
        except ValueError:
            logger.warning("[typography] unparseable content in config %s", config_doc_id)
            return {}
        table = parsed.get("replacements", parsed)
        if not isinstance(table, dict):
            return {}
        return {
            str(k): str(v) for k, v in table.items()
            if isinstance(k, str) and k.strip()
        }

    return {}


def _validate_interval_list(value: object) -> list[dict] | None:
    """Validate a list of {name, min?, max?} interval dicts.

    Returns the normalised list, or None when the shape is wrong anywhere —
    the caller drops the whole top-level field and warns (field granularity:
    a half-parsed table is a config author's error, not a partial win).
    """
    if not isinstance(value, list) or not value:
        return None
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        interval: dict = {"name": name}
        for bound in ("min", "max"):
            raw = entry.get(bound)
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None
            interval[bound] = raw
        out.append(interval)
    return out


def _normalize_ranges_table(value: object) -> list[dict] | dict[str, list[dict]] | None:
    """Normalise one top-level ranges field: flat list or keyed sub-tables.

    A dict field is a CONDITIONAL table: sub-tables keyed by string (int keys
    are stringified so `0:` and `"0":` in YAML both address "0"), with an
    optional `by: <variable name>` documentation key that is dropped on load.
    Returns None when the shape is wrong.
    """
    if isinstance(value, list):
        return _validate_interval_list(value)
    if isinstance(value, dict):
        sub_tables: dict[str, list[dict]] = {}
        for k, v in value.items():
            if k == "by":
                continue
            intervals = _validate_interval_list(v)
            if intervals is None:
                return None
            sub_tables[str(k)] = intervals
        return sub_tables or None
    return None


async def resolve_ranges_doc(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> dict[str, list[dict] | dict[str, list[dict]]]:
    """Find the `ranges` child doc and parse its interval tables.

    ARCH: reference intervals (norm bounds) are a DETERMINISTIC compute input
    for `range()` in `calculate:` (backend/pipeline/extractor/compute.py), not
    a prompt rule — the model never sees this doc. Same posture and same
    measured reason as `typography` (see resolve_typography_doc): the `dictionary`
    child is a prompt slot whose measured cost of a misused table is the 8-field
    shift recorded there. Content reaches the prompt ONLY through a literal
    `{ranges}` placeholder in a custom `prompt:` — the shipped default has none.

    Shape (yaml in a code block, same as the other config children):

        uterus_length:                 # flat table
          - {name: ниже, max: 40}
          - {name: норма, min: 40, max: 60}
          - {name: выше, min: 60}

        m_echo_thickness:              # conditional: sub-tables per key value
          by: postmenopause            # documentation only, dropped on load
          "0":
            - {name: норма, max: 15}
            - {name: выше, min: 15}
          "1":
            - {name: норма, max: 5}
            - {name: критично, min: 5}

    Returns {} when the doc is absent, empty, or unparseable. A top-level field
    of the wrong shape — or whose key is not a Python identifier, because
    range() parses table names as code — is dropped with a warning naming it;
    the other fields survive.
    """
    if rows is None:
        rows = await fetch_child_rows(config_doc_id)

    for row in rows:
        if (row.get("title") or "").strip().lower() != "ranges":
            continue
        content = (row.get("content") or "").strip()
        if not content:
            return {}
        return _ranges_tables_from_content(content, config_doc_id)

    return {}


def _ranges_tables_from_content(
    content: str, config_doc_id: str
) -> dict[str, list[dict] | dict[str, list[dict]]]:
    """Parse ranges-doc content and normalize each top-level field.

    Unparseable content yields {} with a warning; per-field drops are decided
    here (non-identifier key, wrong shape) and leave the other fields standing.
    """
    try:
        parsed = _parse_yaml_from_code_block(content)
    except ValueError:
        logger.warning("[ranges] unparseable content in config %s", config_doc_id)
        return {}
    tables: dict[str, list[dict] | dict[str, list[dict]]] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.isidentifier():
            logger.warning(
                "[ranges] dropping table %r in config %s: table names must be "
                "identifiers (letters/digits/underscore) — range() parses them "
                "as code",
                key, config_doc_id,
            )
            continue
        table = _normalize_ranges_table(value)
        if table is None:
            logger.warning(
                "[ranges] dropping field '%s' in config %s: expected a list of "
                "{name, min, max} intervals or a dict of such lists keyed by "
                "sub-table name",
                key, config_doc_id,
            )
            continue
        tables[key] = table
    return tables


async def resolve_instructions_doc(
    config_doc_id: str,
    *,
    rows: list[dict] | None = None,
) -> str:
    """Find the instructions child document under a config doc.

    Looks for child docs titled "instructions".
    Returns instructions string, or empty string if not found.
    """
    inst_names = ["instructions"]

    if rows is None:
        rows = await fetch_child_rows(config_doc_id)

    for row in rows:
        title = (row.get("title") or "").strip().lower()
        content = row.get("content") or ""
        if title in [n.lower() for n in inst_names]:
            return content.strip()

    return ""
