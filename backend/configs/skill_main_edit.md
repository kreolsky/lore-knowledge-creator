---
name: main-edit
description: Changing documents — create, edit, append to and rename one, copy old_string from the text you just read, and how this editor writes embeds, highlights and tables. Use whenever the request asks for a change to a document; «сохрани», «добавь в документ», «исправь», «переименуй», «сделай документ».
tools:
  - create_document
  - edit_document
  - append_to_document
  - rename_document
---

CHANGING A DOCUMENT
- Copy old_string verbatim from the text you just read, short enough to occur once.
- Restructure or rewrite with many small non-overlapping edits in ONE edit_document
  call per document per turn.
- create_document makes a genuinely NEW document; a rewrite routed through it leaves
  the original sitting there.
- When a write fails, read_document and retry once with a fresh old_string; if it
  fails again, say what happened and ask.

HOW THIS EDITOR WRITES THINGS
- A leading ! inlines the whole target into the page instead of linking to it, one
  level deep. {embed_schemes}.
- Highlight text with an inline code span that opens with a color:
  `#fdd663 highlighted text`. The editor offers five:
    #ec883c orange   #8ab4ff blue   #8ab440 green   #a978d6 purple   #fdd663 yellow
  Use #fdd663 when the user names no color, and match the color a category already
  carries in that document.
- Copy an existing highlight character for character when you edit around it: rewrite
  its color token and it turns back into ordinary code.
- A pipe table you write into a document body becomes an editable table object —
  write whole tables on their own lines. To name a table, place it at the end of a
  specific section, or change the cells, rows or columns of an existing one, load the
  `tables` skill first.
