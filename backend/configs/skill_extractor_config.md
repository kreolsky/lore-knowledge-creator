---
name: extractor-config
description: Author and edit extractor pipeline configs (variables and template documents) from the chat. «сделай конфиг экстрактора», «создай variables из template», «добавь переменную в экстрактор», «как устроен конфиг экстрактора»; extractor config, add an extraction field.
tools: []
---

# Extractor config

An extractor config is DOCUMENTS, not files: one parent document plus children found by
TITLE (case-insensitive). This skill is the grammar the code enforces.

## Layout

| Document | Required | Format |
|----------|----------|--------|
| parent (any title) | yes | optional YAML block with `pipeline:` and `prompt:` |
| child `variables` (or `values`) | yes | YAML block with field definitions |
| child `template` | yes | fenced Markdown with `{{name}}` |
| child `instructions` | no | free Markdown, the WHOLE document (not fenced) |
| child `typography` | no | YAML block with a `replacements:` table |
| child `ranges` | no | YAML interval/mark tables — bound to variables, or `range()` in `calculate:` |
| any other child title | no | free text, injected into the prompt as `{title}` |

Config is read from the FIRST fenced code block of a document; prose around it is
ignored (`instructions` excepted). A custom `prompt:` in the parent must keep
`{variables}` — the only channel that carries field descriptions to the model. Most
configs omit the parent YAML.

## Variables

A bare string is the description. The dict form declares more:

```yaml
variables:
  client_name: "Client full name from the inspector's speech."

  floor_finish:
    description: "Floor finish as described."
    default: "Не указано"

  temperature:
    type: number
    description: "Air temperature in °C."
    default: "0"

  building_age:
    type: integer
    description: "Building age in years."

  occupied:
    type: boolean
    description: "The premises are occupied, as stated by the inspector."

  comment:
    type: string
    description: "Free-text remark."

  window_side:
    type: enum
    description: "Side the windows face."
    options: [север, юг, восток, запад]

  method:
    type: multiselect
    description: "Inspection method(s)."
    options: [визуальный, инструментальный]
    separator: " и "

  ventilation:
    type: prefix
    description: "Ventilation state; the inspector may add a measurement."
    options: [работает, не работает]

  status:
    description: "Document status."
    enum: [draft, review, published]

calculate:
  room_volume: "={{room_length}} * {{room_width}} * {{room_height}}"
```

- `description` is the ONLY per-field instruction the model reads — write it as an
  extraction instruction, not a label.
- `default` (a string, any type) is substituted when extraction comes back empty.
- `enum` = one of a closed list, ASR slips snapped to the closest option;
  `multiselect` = several, joined with ", " (or `separator`); `prefix` = a chosen start
  plus a free dictated tail. All three REQUIRE a non-empty `options` list.
- A bare `enum:` key (as on `status`) is the legacy alias for type: enum — never
  combine it with a type word.
- `calculate:` outputs start with `=`. Operators `+ - * / **`, unary `-`; functions
  `round(x)`, `round(x, ndigits)`, `abs(x)`. Inputs coerce to float — keep them
  type: number with a numeric default like `"0"`. A cycle fails the run.
- Per-value marks (a flag, a colour, bold) come from `ranges` tables bound to the
  variable (next section) — no `calculate:` line needed.
- `range({{value}}, table[, {{key}}])` in `calculate:` returns the NAME of the interval
  the value fell in:

  ```yaml
  temperature:                  # flat table
    - {name: ниже, max: 18}
    - {name: норма, min: 18, max: 24}
    - {name: выше, min: 24}

  humidity:                     # conditional — one sub-table per key value
    by: heating_season          # range() ignores by: — the key is its 3rd argument
    "0":
      - {name: норма, max: 60}
      - {name: выше, min: 60}
    "1":
      - {name: норма, max: 45}
      - {name: критично, min: 45}
  ```

  ```yaml
  calculate:
    temperature_flag: "=range({{temperature}}, temperature)"
    humidity_flag: "=range({{humidity}}, humidity, {{heating_season}})"
  ```

  A table name is letters, digits, underscore (`air-temp` is dropped with a warning).
  `key` is always a `{{var}}`, never a literal. Interval, key-matching and empty-value
  rules are the bound-table rules below. An unknown table, or a key given to a flat
  table / missing on a conditional one, fails the run. The flag is a string — never
  feed it into arithmetic. To print a value styled, prefer a bound table's `view:`.

## Ranges tables bound to a variable

A table in the `ranges` child whose name equals a variable (or a `calculate:` output)
is BOUND to it. Each row is a CONDITION plus OUTPUT ATTRIBUTES; every attribute key
becomes a template variable `<variable>_<key>`, computed after extraction and
`calculate:` (the model never sees the table).

```yaml
temperature:                         # bound to the variable temperature
  - {flag: ниже нормы, color: "8ab4ff", view: "{{value}}",     max: 18}
  - {flag: норма,      color: "8ab440", view: "{{value}}",     min: 18, max: 24}
  - {flag: выше нормы, color: "ec883c", view: "**{{value}}**", min: 24}
  - {view: "{{value}}"}              # catch-all: no condition, matches anything
```

The template then writes `{{temperature_view}} °C ({{temperature_flag}})`; for 28
that renders `**28** °C (выше нормы)`, and `{{temperature_color}}` is `ec883c`.

Row keys:

| Key | Role |
|-----|------|
| `min` | condition: value ≥ min (inclusive) — numbers only |
| `max` | condition: value < max (exclusive) — numbers only |
| `is` | condition: value equals ANY listed option — a YAML list, enum variables only |
| `name` | what `range()` returns — generates no variable |
| anything else | an output attribute → `<variable>_<key>` |

How a row is chosen:

- Rows are tried TOP TO BOTTOM; the FIRST match wins.
- A row with no `min`/`max`/`is` is the catch-all — put it LAST. A table whose
  attributes use `{{value}}` MUST end with one, or an out-of-table value vanishes from
  the report.
- No match, or an empty value → every attribute is `""`; so is an attribute the
  matching row lacks.

What an attribute holds:

- A plain string or number. `{{value}}` is replaced by what `{{variable}}` prints; no
  other `{{…}}` is allowed.
- Quote YAML booleans (`yes`, `no`, `on`, `off`, `true`, `false`): `flag: "yes"`.
- Key names are letters, digits, underscore (`flag`, `color_hex`).
- Generated names work in the TEMPLATE only: `calculate:` runs before the marks, so
  `={{temperature_flag}}` fails. Typography runs after and applies to them.

### `is:` — marks by an enum value

`is:` matches an `enum` variable by its snapped option. ALWAYS a list, even for one
value (`is: [сырость]`), every value one of the `options`. A table is numeric
(`min`/`max`) or categorical (`is`), never both.

```yaml
variables:
  wall_state:
    type: enum
    description: "Wall state."
    options: [норма, трещины, сырость]
```

```yaml
wall_state:
  - {is: [трещины, сырость], flag: дефект, view: "**{{value}}**"}
  - {is: [норма], flag: норма, view: "{{value}}"}
  - {view: "{{value}}"}              # catch-all, for an option added later
```

`сырость` → `{{wall_state_flag}}` = `дефект`, `{{wall_state_view}}` =
`**сырость**`.

### `by:` — a different table per value of another variable

When the norm depends on another variable: `by:` names the selector, every other key
is a VALUE of it holding an ordinary list of rows.

```yaml
variables:
  humidity:
    type: number
    description: "Relative humidity in %."
  heating_season:
    type: boolean
    description: "The heating season is on."
```

```yaml
humidity:
  by: heating_season                 # the selector variable
  "0": [{flag: норма, max: 60}, {flag: выше нормы, min: 60}]   # heating_season = false
  "1": [{flag: норма, max: 45}, {flag: критично, min: 45}]     # heating_season = true
```

A number or boolean selector addresses `"0"`/`"1"` (`false`, `0`, `0.0` → `"0"`); any
other value (an enum option) must equal the key exactly. An empty selector, or a value
no sub-table names, leaves every attribute `""`. Each sub-table using `{{value}}`
needs its own catch-all.

### When a table binds, and what fails

A table binds ONLY when its name is a declared variable or `calculate:` output AND its
rows carry an attribute; otherwise it stays a plain `range()` table. A row may carry
`name` and attributes together.

Breaking any rule above on a bound table — and `min`/`max` on an enum, prefix,
multiselect or boolean variable, a `by:` naming an undeclared variable, or a
generated name colliding with a variable or another table's name —
FAILS THE RUN before extraction; the reason is in a Pipeline Error note on the source
document. `min`/`max` on a `string` variable is allowed (read as a number).

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

A per-field override beats the anchor; the duplicate-key Pipeline Warning it trips is
cosmetic.

## Template

Markdown in a fence. Formatting (bold, line breaks, the `N × N × N` layout) lives
HERE, never in descriptions:

```markdown
# {{doc_title}}

**Client:** {{client_name}}
Dimensions: {{room_length}} × {{room_width}} × {{room_height}} m
Volume: {{room_volume}}
```

Builtins — always available, never declare them as fields: {{doc_id}} {{ref_id}}
{{doc_title}} {{ref_title}} {{yyyy}} {{mm}} {{dd}} {{HH}} {{MM}} {{SS}}. An unknown
`{{key}}` renders as an empty string.

## Braces — do not mix

| Surface | Syntax | Example |
|---------|--------|---------|
| template | `{{name}}` | `{{client_name}}`, `{{yyyy}}` |
| calculate refs | `{{name}}` | `={{a}} * {{b}}` |
| prompt | `{name}` | `{transcription}`, `{variables}` |
| any other child title | `{title}` | `{dictionary}` |

## Recipes

Every recipe ENDS by reading the written document back and stating which fields it
declares.

### 1 — variables from a template

`read_document` the `template` child, collect every `{{name}}` minus the builtins, write
one entry each with the description inferred from the surrounding line (no `default`
unless asked). `create_document` titled `variables` under the parent (id from
`get_project_structure`), or `edit_document` an existing one.

### 2 — template from variables

`read_document` the `variables` child; one `## heading` (or bold label) plus the
placeholder per variable, in declaration order; create or edit the `template` child.

### 3 — add a field

Edit BOTH children in ONE turn: the field into `variables` AND the placeholder into
`template`. A field on one side only is the bug.

### 4 — shared defaults

Rewrite N identical default/type pairs into one anchor block as shown above. Show the
before/after in the chat and keep every per-field override.

## Where the config lives

The parent is whatever the user names. Not named? Find a document with a `variables`
child via `get_project_structure`, SAY which you picked before writing, and ask when
ambiguous. A misplaced child is one `move_document` away.

## After writing

No chat tool binds or runs the config: after writing, the user binds it to a source
document and runs it in the editor.
