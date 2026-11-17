---
name: skill-authoring
description: Use when the user asks to keep HOW this was done as a reusable skill, or to improve one. «сделай из этого скилл», «сохрани как навык», «запомни, как это делать», «улучши этот скилл»; make this a skill, improve this skill.
tools: []
---

# Skill authoring

The chat just finished a task; the user wants the METHOD kept, not the result, so
the next model loads a recipe instead of re-deriving it. `save_skill` owns validity
(frontmatter, placement, upsert); you own quality.

## What is worth saving

A procedure the next model would not guess: a non-obvious sequence of tool calls, a
source with quirks that cost a failed attempt, a result the user asked for twice. A
one-step lookup ("search, then say") is not a skill — refuse nothing, but keep the bar
real.

Save the PROCEDURE, never the answer. A fact from this turn goes in only when the
next run needs it to execute — "the API's field is `stargazers_count`" is procedure;
"this repo has 42k stars" is not.

## The two shapes

**Shape A — procedure only.** No script ran, or it is trivial: `save_skill` with
`name`, `description`, `body`; omit `spec` and `tools`.

**Shape B — a sandbox script ran.** `tools: ["sandbox_bash", "sandbox_fetch_skill"]`,
`files` attaching the script, `spec` filled, and the body's Steps carry the run block
(all below). The sandbox pack is already active in this chat, so you can write the
script file with `sandbox_bash` before saving.

## The description: a trigger, not a summary

The model loads a skill when the line matches the REQUEST, not the topic:

```
Use when the user <asks for this kind of work> — «русская фраза», «другая русская фраза», EN phrase, another EN phrase.
```

Use the phrases the USER would type, Russian included — quote this chat's request
when you can.

## The body: four sections

```
# <Title>

One paragraph: what this procedure produces and when it beats improvisation.

## When
The requests this skill answers — the trigger cases, and the near-miss that
should NOT load it.

## Inputs
What the model must collect before starting: the parameter the user gives,
the id or URL to ask for, the read_document call that fills the gap.

## Steps
The ordered procedure, each step naming its tool call.

## Output
What the finished turn hands back: the numbers, the document, the sentence.
```

## Shape B: the script and the run block

The script has ONE home: a child document titled `scripts/run.py`, passed as
`files: [{path: "scripts/run.py", sandbox_path: "<where you wrote it>"}]`. Not a file
yet → write it NOW with `sandbox_bash` to a RELATIVE path such as `work/run.py` (the
shell starts in the workspace; `/workspace/...` is not it) and save with that path.

The saved body's Steps start with this block, verbatim:

```
1. `sandbox_fetch_skill <name>` — run `<returned path>/scripts/run.py` with
   `sandbox_bash` and read the output.
```

The fetch comes BEFORE any `sandbox_bash`, every time — another member has no copy
of your workspace. Never rebuild the script from the Spec or from memory.

## The Spec

The facts a fresh chat cannot re-derive — URL patterns, auth quirks, response field
names, request shapes that failed. Never the script (a copy would go stale). The body
says when to `read_document` the Spec.

## The save call

- Improving a skill («улучши этот скилл») is the SAME call with the same `name`. Omit
  `spec` unless the Spec itself changes — a description tweak must not wipe it.
  Re-attach `files` only when the script changed (fetch it, edit, save the edited
  path).
- A shipped skill's name is refused; pick another.
- Binaries and large data go through `import_file` as references, never `files`.
- Never write the frontmatter yourself or `create_document` into the Skills folder —
  a malformed head is silently skipped by the catalog.

Then report: the skill name, the phrase that loads it, and for Shape B that the
script is attached as `scripts/run.py`. Nothing else.
