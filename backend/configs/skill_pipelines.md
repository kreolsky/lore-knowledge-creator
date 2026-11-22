---
name: pipelines
description: Use when the user asks to bind, configure, run, list or remove an extractor or digest pipeline (a binding, not the config document itself), or to check why a pipeline did or did not run. «привяжи пайплайн», «подключи экстрактор к документу», «запусти дайджест», «какие пайплайны есть», «почему не сработал пайплайн»; bind pipeline, run extractor, pipeline runs, why did the pipeline not run.
tools:
  - list_pipelines
  - bind_pipeline
  - unbind_pipeline
  - run_pipeline
  - pipeline_runs
---

# Pipelines

Bind, run and inspect the project's pipelines from chat: extractor bindings
(new transcriptions of a source document's references extracted into child
documents) and digest bindings (periodic theme reports). Authoring the CONFIG
document is the extractor-config / digest-config skills' job; this skill is
the hands that bind and run them.

## The order

1. `list_pipelines` first — name what exists before adding to it. A binding on
   the same source + kind is updated in place, so "configure" is a re-bind,
   never a duplicate.
2. `bind_pipeline` (document, kind, config doc, target doc; `schedule` for a
   digest). The user sees an approval card — binding IS consent: every future
   run (each new transcription; each schedule tick) runs under it.
3. Run once to prove it: `run_pipeline` on ONE reference (extractor) or now
   (digest).
4. `pipeline_runs` to verify: `event`/`manual`/`schedule` → `running` → `ok`
   with an `output_doc_id`, or `error` with the message.

## Extractor vs digest

- Extractor runs PER REFERENCE: each new transcription of the source's
  references fires one run; `run_pipeline` needs a `reference_id` attached to
  the binding's source document. No schedule — there is nothing to run without
  a reference.
- Digest runs PER BINDING: on its schedule and on new references of the theme
  document; `schedule: {interval_s}` upserts the one schedule row,
  `enabled: false` pauses it, `null` leaves it. A digest may read its
  subjects' chats (`subject_user_ids`).

## Reading a failed run

`pipeline_runs` shows `error` + the message (a broken config document —
invalid YAML, a missing variable — names the config doc and the line class).
The pipeline also leaves a Pipeline Error note on the source/theme document —
the old trace; both stay. `skipped` is not a failure: the pipeline ran and
there was nothing new to write.

## Unbinding

`unbind_pipeline` removes the binding; a digest's schedule goes with it, the
written documents stay. Do not unbind to "fix" a config — fix the config
document; the binding keeps pointing at it.
