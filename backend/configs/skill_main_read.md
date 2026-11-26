---
name: main-read
description: Reading this project — when to answer from what you already have and when to open documents first (read_document, get_project_structure, search_materials), and how a link to a document, a reference or the web is written. Use whenever a question is about what this project says or a document is named; «открой документ», «что у нас есть про», «найди в проекте».
tools:
  - read_document
  - get_project_structure
  - search_materials
---

WHAT YOU ALREADY HAVE, AND WHAT TO GO AND READ
- The documents attached to this turn are in this prompt in full, as the live text on
  screen right now. Answer from those.
- A question you can answer from general knowledge, this conversation or the attached
  documents — a definition, advice, work on pasted text — gets its answer straight
  away, with no tool calls.
- A question about what THIS project says — a document the user names, "what do we
  have on X", a fact of this world — gets read first: get_project_structure shows the
  tree around the open document, read_document opens a node by id. search_materials
  is for material nobody has named yet.
- When you cannot tell which of the two it is, answer from what you have and offer, in
  one sentence, to check it against the project's documents.
- A source or boundary the user names in the request holds for the whole turn and
  outranks this reading advice.
- Anything you saw earlier may have changed since: read a document again right before
  you edit it or build an answer on it.

HOW THIS EDITOR WRITES THINGS
- Link to a document with [text](<id>) — the bare id. Link to a reference with
  [text](ref:<id>). Link to the web with [text](https://…). An id that does not exist
  renders as a broken link.
