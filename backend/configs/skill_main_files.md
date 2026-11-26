---
name: main-files
description: Files of documents — save a file as a reference (import_file) and look at an image reference (view_reference_image). Use when a file or an image is attached to the chat, or the user asks to save or look at one; «сохрани файл», «загрузи аудио», «что на картинке».
tools:
  - import_file
  - view_reference_image
---

FILES
A reference is a node whose content comes from a stored file. Its text is derived in
the background: right after the import read_document shows `processing_status`
`queued` or `processing` — say that it is being read, and read again for the result.
- Never write a file's bytes into a tool call. A file the user dropped on this chat is
  in the sandbox workspace already, and an image attached to a message is addressed by
  the `[attached images: …]` marker under it — both are saved with import_file.
- An image reference is seen with view_reference_image, not read: its id is in a
  document's `references[]`. Use it whenever the picture itself matters.
