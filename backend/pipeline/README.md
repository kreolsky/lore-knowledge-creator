# Pipelines

PocketFlow-based pipelines that consume project documents and emit derived documents. Today only one pipeline ships — `extractor` — but the layout is designed for additional pipeline types under `pipeline/<name>/`.

## Layout

```
pipeline/
├── core/                  # shared, pipeline-agnostic
│   ├── config.py          # YAML/template/variables/instructions resolution from docs
│   └── constants.py       # DEFAULT_PROMPT, DEFAULT_TITLE_TEMPLATE
└── extractor/             # one pipeline = one package
    ├── flow.py            # SetupNode >> ExtractionNode >> ComputeNode >> RenderNode
    ├── nodes.py           # PocketFlow AsyncNode classes
    ├── compute.py         # safe AST evaluator for calculate: expressions
    ├── params.py          # shared reference→agent_configs resolver (editor + MCP)
    ├── runner.py          # entry points: run_extractor, on_transcription_complete
    └── utils.py           # build_json_schema, call_llm_structured, render_template, render_title_template, render_prompt
```

`core/` must not import from any specific pipeline package. Pipelines may import from `core/`.

## How a pipeline is configured

A pipeline is wired up by **documents in the database**, not by JSON in the repo (the legacy `extractors.json` was removed). For the extractor pipeline you need:

1. **Source document** — the doc whose references trigger extraction.
2. **Config document** — has a fenced YAML code block at the top with pipeline settings:
   ```yaml
   pipeline: extractor
   prompt: |
     ...optional override of DEFAULT_PROMPT...
   ```
   If `prompt` is omitted, `core/constants.DEFAULT_PROMPT` is used. A custom `prompt` must
   keep `{variables}` — see the `INVARIANT:` above `DEFAULT_PROMPT` (schema descriptions are
   stripped by the llama-server grammar, so the prompt is the only channel for them).
3. **Child documents** of the config doc — looked up by title (case-insensitive):
   - `template` — fenced markdown block; rendered with **`{{name}}`** double-brace placeholders from extracted/calculated data, plus system placeholders `{{doc_id}}`, `{{ref_id}}`, `{{doc_title}}`, `{{ref_title}}` and UTC time `{{yyyy}} {{mm}} {{dd}} {{HH}} {{MM}} {{SS}}`.
   - `variables` (or `values`) — fenced YAML with field definitions. Drives JSON-schema generation and `{variables}` substitution in the prompt.
   - `instructions` — free-form markdown, substituted as `{instructions}` in the prompt.
   - `typography` — fenced YAML `replacements:` table; applied deterministically AFTER `calculate:` (not a prompt slot).
   - Any other child doc title becomes a `{title}` placeholder for `render_prompt`.

> **Full config reference (field types `enum`/`multiselect`/`prefix`, `default:`, `calculate:`
> syntax, placeholder cheat-sheet, what reaches the LLM): see
> [`docs/pipelines/extractor/configuration.md`](../../docs/pipelines/extractor/configuration.md).**
> The config sections previously duplicated in this README were stale; that file is now the
> single source of truth.
4. **`agent_configs` row** — links source doc → config doc → target doc, with `trigger_event` (e.g. `transcription_complete`) and optional `title_template`, `model`. Multiple configs per document are allowed (one per `trigger_event`); enforced by index `idx_agent_configs_doc_trigger`.

## Triggering

- **Event hook**: `runner.on_transcription_complete` is wired into the event bus. When a reference's transcription finishes, every matching `agent_configs` row enqueues `extract_task` on the arq worker.
- **Manual**: the REST manual trigger (`routes/extractor.py`) enqueues the same `extract_task`.

Task failures create an error note attached to the source document via `_create_error_note` — never silent.

## Adding a new pipeline

1. Create `pipeline/<name>/` with `flow.py`, `nodes.py`, `runner.py`, `utils.py`.
2. Reuse `core/config.py` resolvers — they are pipeline-agnostic.
3. If the new pipeline needs new defaults, extend `core/constants.py` (don't hardcode in nodes).
4. If it triggers off a new event, write a handler in `runner.py` that filters `agent_configs` by `trigger_event = '<event>'`, and subscribe it in `event_bus` setup (see how `on_transcription_complete` is registered). There is no event allowlist constant — `trigger_event` is a free-form field on the row.
5. Schema: if storage shape differs from `agent_configs`, add a new table in `surreal/schema.surql`. If you only need a new `trigger_event`, no schema change is required.
6. Frontend: agent-config UI lives in `frontend/src/components/AgentConfigManager.tsx` — driven by the `trigger_event` field.

## Title rendering

`render_title_template(template, source_title, reference_title=, tz_name=)` supports placeholders `{yyyy}`, `{mm}`, `{dd}`, `{HH}`, `{MM}`, `{title}` / `{doc_title}`, `{ref_title}`, `{rnd:N}`. Timezone resolution: user's `users.timezone` (IANA) → UTC fallback. Failures in tz lookup degrade to UTC, never raise.

## Tests

- `tests/backend/test_extractor.py` — runner + hook integration.
- `tests/backend/test_extractor_utils.py` — pure-function tests (schema build, prompt render, title render, LLM call mock).

Run inside Docker:
```
docker compose exec backend pytest /tests/backend/test_extractor.py /tests/backend/test_extractor_utils.py
```

## Variables YAML format

The authoritative reference for `variables` YAML — field types
(`string`/`number`/`integer`/`boolean`/`enum`/`multiselect`/`prefix`), `options:`, `default:`,
the legacy `enum:` alias, duplicate-key handling, `calculate:` expressions, and the three
distinct placeholder syntaxes — is
[`docs/pipelines/extractor/configuration.md`](../../docs/pipelines/extractor/configuration.md).

Highlights (see the reference for full detail):
- `enum`/`multiselect`/`prefix` are declared via `type:` + `options:`. `enum`/`prefix` are
  **free-string transcription + deterministic canonicalization** (not an LLM grammar
  constraint); `multiselect` keeps the enum grammar. A bare `enum:` key remains a legacy alias.
- `default:` (any type, string) substitutes on empty extraction. `number`/`integer` widen to
  nullable in the schema so a default can fire on absence.
- `calculate:` expressions start with `=`, reference `{{name}}`, and support `+ - * / **`
  (and unary `-`), `round()`, `abs()` — numbers only, topologically ordered; cycles error.
- `template` uses **`{{name}}`** double braces; `prompt`/`title_template` use **`{name}`**
  single braces. Don't mix them.
