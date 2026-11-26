# Pipelines

A pipeline processes a document's materials in the background and writes its result into a target document. Two kinds: the extractor and the digest. Pipelines are managed in the "References" tab of the source document, section "Pipelines".

## Extractor

Extracts structured fields from materials and fills a template with them: a form, a call summary, a reference sheet.

How to set it up:

1. Describe in a separate document what to extract and in what form (the config document). The agent will help you write it: it has a skill for tuning the extractor.
2. In the "References" tab of the document with the materials, open "Pipelines" and press "Add pipeline". Choose the config document and the target document where the result goes. The pipeline saves itself once both are chosen; a card you no longer need is removed with the bin button.
3. The extractor starts by itself when the transcript of a new recording is ready; there is no other trigger yet. You can run it by hand with the "Send to Pipeline" button on a reference's card.

The card also sets a title template for the result (date, time, document name) and the model. The model list holds only models that support structured output; the administrator controls which ones are offered.

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

## Digest

A running report on one theme (health, a project, a hobby) from your chats and from the materials attached to a theme document. Each run writes one new report: what is new since the previous report and what changed against it.

How to set it up:

1. Write the config document, in any folder. Its first YAML block names the theme and, optionally, keywords and a model; the text after the block says what the report should look like. The agent will help you write it: it has a skill for digests.

```yaml
theme: Health — sleep, wellbeing, pain, injuries, doctor visits
keywords: [sleep, insomnia, pain, knee, blood pressure]
```

2. Open the theme document (for example "Health"), the "References" tab, open "Pipelines", and press "Add pipeline". Choose the "Digest" kind, the config document, and the target document where the reports go.
3. Press "Run now" on the digest's card. After that it also runs by itself when the transcript of a recording attached to the theme document is ready.

What a run reads, each time only what appeared after the previous report:

- the references attached to the theme document;
- your chats opened on the theme document;
- your other chats in the project that match the theme and the keywords.

A digest reads the chats of the person who set it up. A project owner can also choose other members whose chats it reads; their excerpts are signed with their names. The report is a document every project member can see, so a chat you mark "not indexed" never gets into a digest. A member who leaves the project drops out of the digest. Only the digest's author or a project root (see [Access and publishing](help:access)) can change or run it. Deleting a digest is open to its author, a project root, an instance administrator, and the moderator of the group the author belongs to. Stopping or deleting a digest's schedule needs the right to delete it; turning the schedule on or changing its interval needs the right to run it.

Voice notes in the material are signed with who uploaded them and when, like chat lines with their author. If nothing new appeared, a run writes nothing. Deleting a report makes the next run cover its period again. One chat can get into several themes. If a run fails, the reason appears in a "Pipeline Error" system note of the theme document.

Scheduled runs have no setting in the interface yet (see Planned in [Lore Help](help:index)), but the agent sets them: ask it to run the digest every so many hours, or to pause the schedule.

If the theme document or the target document is in the trash, scheduled runs are skipped, and one "Pipeline Skipped" system note appears on the first of them that is still alive — the theme document, then the target, then the config document. Once the document is restored, runs resume by themselves.

## Ask the agent

The agent's `pipelines` skill (see [Skills](help:skills)) does the same as the "Pipelines" section, by request:

- lists the pipelines bound to a document;
- binds an extractor or a digest to a document, or removes a binding;
- runs a pipeline now, and can wait up to 240 seconds for the result;
- sets or pauses a digest's schedule;
- reads the run log and says why a pipeline did or did not run. Each run is recorded with its trigger (an event, a manual run, the schedule) and goes from "running" to "ok" or "error"; "skipped" means there was nothing new to process.

Binding, running and removing are confirmed like the agent's edits; in the Auto mode they apply at once. An external agent on an API key cannot manage pipelines.
