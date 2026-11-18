---
name: structure
description: Use when the user wants to move or reorder documents in the tree, turn a document into a reference or back, see what links to or from a document, or re-run the import of a reference whose text is wrong or empty. «перенеси», «перемести в папку», «поменяй порядок», «сделай референсом», «что ссылается на этот документ», «битые ссылки», «переобработай референс», «перераспознай запись»; move this document, what links here, reprocess this reference.
tools:
  - move_document
  - get_links
  - reprocess_reference
---

# Project structure

## Moving and reordering

`move_document` moves a tree document or a reference: `parent_id` is the new parent,
`after_id` the sibling it lands after (null = first; tree documents only). To reorder in
place, pass the current `parent_id`. The project's index document as the parent means the
project root. Get both ids from `get_project_structure` first; ask where it should go if
the user did not say.

A reference is visible from its host and the host's descendants — host it on the index
document when the whole project should reach it. `node_type` converts in the same call: a
document becomes a reference of `parent_id` (refused if it has children), or a reference
becomes a tree document (refused for a file-backed one). A move is not undone from the
History panel — undo it by moving back.

## Links

`get_links` returns one document's links in both directions — what the user's links panel
shows: mentions, transclusions, attachments, pinned notes and outgoing web links.
`broken` marks a mention or transclusion whose target is deleted. Use it before moving,
renaming or deleting something others point to, and to answer "what links here".

## Reprocessing a reference

When a reference's text is wrong or empty, `reprocess_reference` re-runs its import: audio
is transcribed again, a .docx-backed reference converted again. Never rebuild the text
yourself. It wipes the current text and the new one arrives later, so after it applies,
tell the user and end the turn. Images cannot be reprocessed — there is no OCR.
