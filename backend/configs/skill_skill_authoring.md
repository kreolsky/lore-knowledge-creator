---
name: skill-authoring
description: Use when the user asks to keep HOW this was done as a reusable skill — «сделай из этого скилл», «сохрани как навык», «запомни, как это делать», «улучши этот скилл», make this a skill, save this as a skill, improve this skill. Extracts the reusable procedure from the finished turn and saves it with one save_skill call.
tools: []
---

# Skill authoring

The chat just finished a task. The user wants the METHOD kept, not the result:
next time the same request comes, the model should load a recipe instead of
re-deriving it. `save_skill` writes the document; this skill decides WHAT goes
into it. The tool owns validity (frontmatter, placement, upsert); you own
quality.

## What is worth saving

A procedure deserves a skill when it would have to be re-derived AND the
re-derivation is non-obvious: a sequence of tool calls the next model would
not guess, a source with quirks that cost you a failed attempt, a result the
user asked for twice. A one-step lookup ("search, then say") is not a skill —
refuse nothing, but keep the bar real: a catalog of thin skills trains the
user to stop asking.

Save the PROCEDURE, never the answer. Numbers, names and findings from this
turn go in the Spec (below) only when the NEXT turn needs them to execute the
procedure — "the API's field is called `stargazers_count`" is procedure;
"this repo has 42k stars" is not.

## The two shapes

**Shape A — procedure only.** No script ran, or the script is trivial. Call
`save_skill` with `name`, `description`, `body`; omit `spec` and `tools`.

**Shape B — a sandbox script ran.** The work's core is a script in the
console. Then: `tools: ["sandbox_bash", "sandbox_fetch_skill"]` (the pack the
saved skill activates), `files` attaching the script (below), `spec` filled
(below), and the saved body's Steps carry the run block (below). The sandbox
pack is already active in THIS chat — a script ran, and packs accumulate per
chat — so you can use `sandbox_bash` right now to write the script file
before saving.

## The description: a trigger, not a summary

The description is the one line the catalog shows; the model loads the skill
when it matches the REQUEST, not when it matches the topic. Copy this shape:

```
Use when the user <asks for this kind of work> — «русская фраза», «другая русская фраза», EN phrase, another EN phrase.
```

Write the phrases the USER would actually type — including Russian ones — not
your own vocabulary. Quote them from this chat's request when you can.

## The body: four sections

The body is what the model reads on load. Copy this shape:

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

The script has ONE home: a child document of the skill titled
`scripts/run.py`, stored verbatim. `save_skill` reads it from your workspace
— pass `files: [{path: "scripts/run.py", sandbox_path: "<where you wrote
it>"}]`. If the script is not a file yet, write it NOW with `sandbox_bash`
to a path INSIDE your workspace — a relative path such as `work/run.py`
(the shell starts in the workspace; `/workspace/...` is not it) — then save
with that same relative path.

The saved body's Steps start with this fixed block, verbatim:

```
1. `sandbox_fetch_skill <name>` — run `<returned path>/scripts/run.py` with
   `sandbox_bash` and read the output.
```

The fetch is called BEFORE any `sandbox_bash`, every time: it materializes the
skill's files at a deterministic directory for whoever is running it (another
member has no copy of your workspace). Never rebuild the script from the Spec
or from memory — the fetch alone restores it.

## The Spec: what cannot be re-derived

`spec` is stored as a child document the skill lists under `## Material` but
does not load — one line per load; the body says when to `read_document` it.
That is why the spec lives there and NOT in the body: the body rides every
load of this skill, the Spec only when it is needed.

Put in it the FACTS a fresh chat cannot re-derive — URL patterns, auth
quirks, response field names, request shapes that failed, pitfalls that cost
an attempt. Never the script: it lives in `files`, and a copy here would go
stale the first time the file changes.

## The save call

One `save_skill` call, then tell the user the skill's name and which phrase
loads it.

- Improving an existing skill («улучши этот скилл») is the SAME call with the
  same `name` — the server updates the existing document in place. Omit
  `spec` unless the Spec itself must change; a description tweak must not
  wipe it. Re-attach `files` only when the script changed (fetch it first,
  edit, save with the edited path); omitted files stay as they are.
- `name` is kebab-case. `tools` names must be served tools — the console is
  `sandbox_bash`, not `bash`. A shipped skill's name is refused; pick another.
- Binaries and large data go through `import_file` as references, never
  through `files` — a skill file is text, and small.
- Never write the frontmatter yourself and never `create_document` a file into
  the Skills folder — a malformed hand-written head is silently skipped by the
  catalog.

## Output

Report: the skill name, the trigger phrase that loads it, and — for Shape B —
that the script is attached as `scripts/run.py` and `sandbox_fetch_skill`
materializes it. Nothing else; the save already said what landed.
