"""Default values for pipeline configs.

User-provided YAML in config-documents overrides these defaults.
"""
# SYSTEM: pipeline-constants — default values for all pipeline configs

# INVARIANT: the prompt MUST keep {variables} (or its {fields} alias).
# Why: it is the ONLY channel that carries the per-field descriptions to the
# model — NOT a duplicate of the JSON-schema `description`.
# Why: the extractor runs against a llama-server backend (AI_API_URL → nnp-llm-router,
# "Local LLM via llama-server"), where `response_format: json_schema` is compiled into a
# GBNF grammar. The grammar encodes STRUCTURE only — keys, types, enum options — and drops
# every `description`. Measured 2026-07-27 on the live endpoint: a field whose schema
# description said "ВСЕГДА возвращай 'PINEAPPLE-42'" returned the transcript text instead,
# while the same instruction placed in the prompt was obeyed and an `enum:` in the same
# schema was enforced. Dropping the section leaves the model with bare field names and
# collapses extraction quality while still emitting schema-valid JSON (observed on a manual
# CIR run). Providers that DO forward schema descriptions (OpenAI) make this a harmless
# duplication — keeping it is safe on both.
DEFAULT_PROMPT = (
    "You are a data extraction assistant. Extract specific fields from the "
    "transcription text below and return them as a JSON object.\n\n"
    "## Fields to extract\n\n"
    "{variables}\n\n"
    "## Rules\n\n"
    "1. Extract the value for each field listed above from the transcription text.\n"
    '2. If a field\'s value is not found in the text, use an empty string ("").\n'
    "3. Your response must be ONLY a valid JSON object — no markdown fences, no explanations, no extra text.\n"
    '4. The JSON keys must be exactly the field names listed above (e.g. "patient_name", not "variables").\n'
    '5. Example: if fields are "name" and "age", return {"name": "John", "age": "30"} — NOT {"fields": {"name": "John"}}.\n\n'
    "## Instructions & Glossary\n\n"
    "{instructions}\n\n"
    "## Transcription\n\n"
    "{transcription}"
)

DEFAULT_TITLE_TEMPLATE = "extractor-{yyyy}-{mm}-{dd} {HH}:{MM} {title}"

BUILTIN_PROMPT_VARS = {"transcription"}

ALLOWED_VARIABLE_TYPES = {"string", "number", "integer", "boolean"}

# The `type:` values that declare a closed `options:` list (categorical fields).
# enum = single forced choice; multiselect = several from the list; prefix = a
# chosen start + a free dictated tail. See SYSTEM: extractor.
CATEGORICAL_VARIABLE_TYPES = {"enum", "multiselect", "prefix"}
