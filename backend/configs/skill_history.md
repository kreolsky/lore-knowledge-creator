---
name: history
description: Use when the user asks about a document's past versions — what changed, who edited, compare with an earlier state, bring back a passage, roll back or restore. «что поменялось», «кто правил», «сравни с вчерашней версией», «верни как было», «откати», «восстанови версию»; what changed, roll back, restore a version.
tools:
  - list_document_versions
  - diff_document_versions
  - restore_document_version
---

# Document history

A document's history is its saved versions (snapshots). Find the one the user means with
`list_document_versions` — by `created_at`, `author`, `label`, or the line counts — and
read it with `read_document(version=<checkpoint_id>)`.

## What changed

`diff_document_versions(from_checkpoint_id=<it>)` compares that version with the current
text. Retell the diff in plain words — what was added, removed, rewritten — never paste
the raw diff unless asked.

## Bringing something back

- **A passage** («верни тот абзац») — read it from the old version and put it back with
  `edit_document`. Everything else in the document stays as it is now.
- **The whole document** («откати всё») — `restore_document_version`. Before the call,
  diff that version against the current text and tell the user what the restore will
  undo: it replaces the whole document for every co-editor, including edits made after
  that version.

A restore saves the state before it and returns `backup_checkpoint_id`; to undo the
restore, restore that one. Name it to the user.
