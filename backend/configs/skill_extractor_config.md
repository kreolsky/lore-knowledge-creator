---
name: extractor-config
description: Author and edit extractor pipeline configs — the variables and template documents — from the chat. «сделай конфиг экстрактора», «создай variables из template», «добавь переменную в экстрактор», «шаблон для экстрактора», «поля извлечения», «одинаковые default для переменных», «как устроен конфиг экстрактора», extractor config, variables from template, add an extraction field.
tools: []
---

# Extractor config

An extractor config is DOCUMENTS, not files: one parent document plus children found by
TITLE (case-insensitive). You already have every tool this skill commands — what it adds
is the grammar the code enforces.

## Layout

| Document | Required | Format |
|----------|----------|--------|
| parent (any title) | yes | optional YAML block with `pipeline:` and `prompt:` |
| child `variables` (or `values`) | yes | YAML block with field definitions |
| child `template` | yes | fenced Markdown with `{{name}}` |
| child `instructions` | no | free Markdown, the WHOLE document (not fenced) |
| child `typography` | no | YAML block with a `replacements:` table |
| child `ranges` | no | YAML interval tables for `range()` in `calculate:` |
| any other child title | no | free text, injected into the prompt as `{title}` |

YAML and template content is read from the FIRST fenced code block of the document —
put the config inside a fence; prose around it is ignored (`instructions` is the one
exception, whole document).

The parent's optional YAML carries two keys at most. A custom `prompt:` must keep
`{variables}` — it is the only channel that carries field descriptions to the model.
Most configs omit the parent YAML entirely.

## Variables

A bare string is the description. The dict form declares more:

```yaml
variables:
  patient_name: "Patient full name from the doctor's speech."

  m_echo_structure:
    description: "M-echo structure as described."
    default: "Не указано"

  uterus_length:
    type: number
    description: "Uterus length in mm."
    default: "0"

  age:
    type: integer
    description: "Patient age in years."

  pregnant:
    type: boolean
    description: "Pregnancy stated by the doctor."

  comment:
    type: string
    description: "Free-text remark."

  uterus_flexio:
    type: enum
    description: "Uterus flexion."
    options: [Anteflexio, Retroflexio, без сгиба]

  access:
    type: multiselect
    description: "Ultrasound access route(s)."
    options: [трансвагинальный, трансабдоминальный]

  cervical_canal:
    type: prefix
    description: "Cervical canal state; the doctor may add a measurement."
    options: [расширен, не расширен]

  status:
    description: "Document status."
    enum: [draft, review, published]

calculate:
  uterus_volume: "={{uterus_length}} * {{uterus_width}} * {{uterus_thickness}} * 0.523"
```

- `description` is the ONLY per-field instruction the model reads — write it as an
  extraction instruction, not a label.
- `default` (a string, any type) is substituted when extraction comes back empty.
- `enum` = one of a closed list, ASR slips snapped to the closest option; `multiselect`
  = several from the list, joined into «A и B»; `prefix` = a chosen start plus a free
  dictated tail. All three REQUIRE a non-empty `options` list.
- A bare `enum:` key (as on `status` above) is the legacy alias for type: enum plus the
  same list — never combine it with an explicit type word.
- `calculate:` outputs start with `=` and reference fields with double braces. Operators
  are `+ - * / **` and unary `-`; functions are `round(x)`, `round(x, ndigits)`,
  `abs(x)`. Inputs coerce to float — keep them type: number with a numeric default like
  `"0"`. A cycle between calculated fields fails the run.
- `range({{value}}, table[, {{key}}])` holds the NAME of the interval the value fell
  in, read from the `ranges` child (see Layout). The model never sees the table — the
  flag is computed deterministically:

  ```yaml
  uterus_length:                # flat table
    - {name: ниже, max: 40}
    - {name: норма, min: 40, max: 60}
    - {name: выше, min: 60}

  m_echo_thickness:             # conditional — one sub-table per key value
    by: postmenopause           # documentation only, dropped on load
    "0":
      - {name: норма, max: 15}
      - {name: выше, min: 15}
    "1":
      - {name: норма, max: 5}
      - {name: критично, min: 5}
  ```

  ```yaml
  calculate:
    uterus_length_flag: "=range({{uterus_length}}, uterus_length)"
    m_echo_flag: "=range({{m_echo_thickness}}, m_echo_thickness, {{postmenopause}})"
  ```

  Grammar rules: a table name is an identifier — letters, digits, underscore (a
  hyphenated key like `m-echo` is dropped with a warning); `key` is a `{{var}}`,
  never a literal (`"1"` and `1` both fail the run); key values are `0`/`1` (any
  numeric spelling, booleans included) or the exact sub-table strings — a value no
  sub-table names (`"да"` against `"0"`/`"1"`) falls through to an empty flag +
  warning. A value on a bound belongs to the interval it opens (`min` inclusive,
  `max` exclusive); a value outside every interval, an empty (or blank) measurement,
  or a missing key yields `""`. An unknown table, or a key given to a flat table /
  missing on a conditional one, fails the run on every row — it is a config error,
  not data. The flag is a string — never feed it back into arithmetic.
  To print a flag value styled, add it to `typography` (e.g. `"критично":
  "**критично**"`) — the dictionary runs after the formulas.

## Shared defaults

When several fields carry the same default/type pair, declare it ONCE as a top-level
anchor next to `variables:` and merge it in:

```yaml
.shared: &common
  default: "не указано"
  type: string

variables:
  size:
    <<: *common
    description: "Размер."

  shape:
    <<: *common
    description: "Форма."

  conclusion:
    <<: *common
    default: "нет данных"
    description: "Заключение."
```

The anchor key is top-level, so the resolver never reads it as a field. A per-field
override still beats the anchor — but it also trips the duplicate-key Pipeline Warning;
that note is cosmetic, the override stands.

## Template

Markdown in a fence. Substitutions use double braces; formatting (bold, line breaks,
the `N × N × N` layout) lives HERE, never in descriptions:

```markdown
# {{doc_title}}

**Patient:** {{patient_name}}
Dimensions: {{uterus_length}} × {{uterus_width}} × {{uterus_thickness}} mm
Volume: {{uterus_volume}}
```

Builtins — always available, never declare them as fields: {{doc_id}} {{ref_id}}
{{doc_title}} {{ref_title}} {{yyyy}} {{mm}} {{dd}} {{HH}} {{MM}} {{SS}}. An unknown
`{{key}}` renders as an empty string.

## Braces — do not mix

| Surface | Syntax | Example |
|---------|--------|---------|
| template | `{{name}}` | `{{patient_name}}`, `{{yyyy}}` |
| calculate refs | `{{name}}` | `={{a}} * {{b}}` |
| prompt | `{name}` | `{transcription}`, `{variables}` |
| any other child title | `{title}` | `{dictionary}` |

## Recipes

Every recipe ENDS by reading the written document back and stating which fields it
declares — your own check that the fence and the YAML survived.

### 1 — variables from a template

`read_document` the `template` child and collect every `{{name}}`, dropping the
builtins listed above. Write one entry per remaining name — the description inferred
from the template line around the placeholder; leave `default` out unless the user
asked for one. `create_document` titled `variables` under the config parent (its id
from `get_project_structure`); when a `variables` child already exists, `edit_document`
it instead.

### 2 — template from variables

The reverse. `read_document` the `variables` child, then write one `## heading` (or
bold label) plus the placeholder per variable, in declaration order. `create_document`
titled `template` under the parent, or `edit_document` when the child exists.

### 3 — add a field

Edit BOTH children in ONE turn: the field into `variables` AND the placeholder into
`template`. A field present on one side only is exactly the bug the user is reporting
when they ask for this.

### 4 — shared defaults

Rewrite N identical default/type pairs into one anchor block as shown above. Show the
before/after in the chat and keep every per-field override.

## Where the config lives

The parent document is whatever the user names. Not named? Find a document with a child
titled `variables` via `get_project_structure`, SAY which document you picked before
writing, and ask when it is still ambiguous. A misplaced child is one `move_document`
away.

## After writing

Binding the config to a source document and running the extraction happen in the
EDITOR UI, not from the chat — no chat tool exists for either. Say it in the positive
form: after writing, the user binds and runs it in the editor.
