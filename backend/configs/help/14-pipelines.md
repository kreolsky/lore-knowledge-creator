# Pipelines

A pipeline is automatic processing of materials tied to a document. It runs in the background and delivers its result as a document.

## Extractor

Lore currently has one pipeline, the extractor. It extracts structured information from the materials and assembles a deterministic report from it: reference data, a summary of calls, a filled-in form.

How to set it up:

1. Describe in a separate document what to extract and in what form (the config document). The agent will help you write it: it has a skill for tuning the extractor.
2. In the "References" tab of the document with the materials, in the "Agents" section, press "Add agent". Choose the config document and the target document where the result goes.
3. The extractor starts by itself when the transcript of a new recording is ready; there is no other trigger yet. You can run it by hand with the "Send to Pipeline" button on a reference's card.

You can set a template for the result's title (with the date, time and document name) and the model.

If processing fails, the details appear in a system note of the document.

## Marks by ranges and values

The extractor can mark values by itself: "normal" or "above normal", a color, a number in bold. For this, the config gets a child document `ranges`, and in it a table named after the variable. Such a table is bound to the variable.

Each row of the table is a condition plus marks. Conditions: `min` (the value is not less than), `max` (the value is less than) and `is` (the value is from a list). All the other keys of a row are marks, and each one becomes a template variable `<variable name>_<key>`. Inside a mark, `{{value}}` inserts the value itself.

```yaml
temperature:
  - {flag: below normal, view: "{{value}}",     max: 18}
  - {flag: normal,       view: "{{value}}",     min: 18, max: 24}
  - {flag: above normal, view: "**{{value}}**", min: 24}
  - {view: "{{value}}"}
```

In the template write `{{temperature_view}} °C ({{temperature_flag}})`; for 28 you get "**28** °C (above normal)".

Rows are checked top to bottom, and the first match wins. A row without conditions always matches — put it last: it catches everything that did not match above. If the marks use `{{value}}`, such a row is required. An empty value, or a value that matched no row, gives empty marks.

**Marks by a list option — `is`.** For a variable with `type: enum`, the `is` condition lists options from its `options`. It is always a list, even for a single option: `is: [damp]`, not `is: damp`.

```yaml
wall_state:
  - {is: [cracks, damp], flag: defect}
  - {is: [normal], flag: normal}
  - {flag: ""}
```

The last row without conditions here leaves the mark empty: this way a value that is not in the list does not get someone else's mark.

**Different norms depending on another variable — `by`.** The `by` key names the switch variable; the other keys are its values, each with its own table. For a number or yes/no these are `"0"` and `"1"`; for a list option, the option itself.

```yaml
humidity:
  by: heating_season
  "0": [{flag: normal, max: 60}, {flag: above normal, min: 60}]
  "1": [{flag: normal, max: 45}, {flag: critical, min: 45}]
```

If a table named after a variable is written incorrectly (for example, `is` without a list, or no last row without conditions), processing does not start, and the reason appears in a "Pipeline Error" system note on the document. The agent knows the full grammar: ask it to set up the marks.

A multiple choice is joined with commas by default ("A, B, C"); the `separator` key in the variable's declaration sets your own string between the values (multiselect only):

```yaml
method:
  type: multiselect
  options: [visual, instrumental]
  separator: " and "
```

## Planned

- Running pipelines on a schedule from the interface (the mechanism exists, but there is no setting in the interface yet).
- New pipelines, for example summarization.
- Pipelines the agent assembles itself to automate recurring work.
