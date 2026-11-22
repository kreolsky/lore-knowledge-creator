---
name: digest-config
description: Author or fix a theme digest config and explain how a digest runs. «сделай дайджест», «настрой дайджест по теме», «почему дайджест пустой», «как работает дайджест»; theme digest, digest config.
tools: []
---

# Theme digest

A digest is a pipeline that writes ONE new report document per run: what is new on a
theme since the previous report, and what changed against it. You author and fix the
config document with your document tools; the binding to a "theme document" is made
with the `pipelines` skill (`bind_pipeline` — it also schedules the digest), so load
it when the user wants the digest bound, scheduled or run.

## The config document

An ordinary document, in any folder. The FIRST fenced YAML block carries the settings;
the prose AFTER the block is the instruction for the report. Nothing else is read — no
child documents.

```yaml
theme: Здоровье — сон, самочувствие, боли, травмы, визиты к врачам
keywords: [сон, бессонница, боль, колено, давление, анализы]
```

| Key | Required | Meaning |
|-----|----------|---------|
| `theme` | yes | one line: the theme and what belongs to it |
| `keywords` | no | a list (or one string) of words the material is likely to use |
| `model` | no | a model id; the binding's model wins over it, the chat default is the fallback |

`theme` plus `keywords` is the search query over the chats: write them in the chats'
language, with the words people actually use. A vague theme pulls in unrelated chats; a
narrow one misses paraphrases — list those as keywords.

The instruction prose says what the report looks like: sections, length, what to
stress. It must not ask to restate everything — the model already gets the previous
report and writes only what is new and what changed.

To create one: `create_document` with the YAML block and the instruction, in the folder
the user names (`get_project_structure` to find it). To fix one: `read_document`, then
`edit_document` — keep the YAML inside its fence.

## Attaching it (the user does this)

1. Open the theme document (for example «Здоровье»), the "References" tab, "Pipelines",
   "Add pipeline".
2. Kind: "Digest". Config document: the one above. Target document: where reports go —
   each run adds a child there. The card saves itself once both documents are chosen.
3. A project owner may also tick whose chats the digest reads. Everyone else's digest
   reads only their own chats.

Only the digest's author or the project owner can change or run it; any other member
can only delete it. If the user cannot save or run a digest, that is why.

## What one run reads

Only material that appeared after the previous report:

- references attached to the theme document (a voice note's transcript included);
- messages in the readers' chats opened on the theme document;
- the readers' other chats in the project that match `theme` + `keywords`.

Chats marked "not indexed", note chats and deleted chats are never read. A member who
left the project is dropped from the readers. One chat can land in several themes.

## When it runs

- "Run now" on the digest's card under "Pipelines".
- By itself when the transcript of a reference attached to the theme document is ready.

## Reading the outcome

- No new document after a run: nothing appeared since the previous report. That is
  normal, not an error.
- A report that says nothing is relevant: new material existed but none of it was on the
  theme. The next run starts after it.
- Deleting a report makes the next run cover its period again.
- A failure leaves a "Pipeline Error" system note on the theme document with the reason.
