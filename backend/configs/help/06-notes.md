# Notes

A note is a discussion thread attached to a document, optionally to a fragment of its text. Comments and replies live in the thread.

## Creating a note

- Select text and press ⌘/Ctrl + M or "Note" on the selection toolbar. The fragment is highlighted in yellow with a dashed underline.
- "+" in the "Notes" tab creates a note not tied to text.

## Working with notes

- All of a document's notes are in the "Notes" tab; an open note shows the whole thread.
- A note can be linked from text: in the ⌘/Ctrl + K picker type `note:`.
- The "Commentator" role cannot edit documents but can write notes — the way to collect remarks from readers.

## Notes that arrive by themselves

- **System notes.** A failed pipeline or agent action posts its details as a new system note of the document.
- **Notes from a widget key.** A text sent over the widget-key API (`POST /api/widget/note`) lands as a note of the key's document, titled with the key's name, and is marked unread for the key's owner (see [Interface](help:interface)). The recording apps do not send notes yet; they upload files.

## Sending a note through a widget key

The copied widget key is base64 of `{"url": …, "token": …}`. Decode it and send `POST <url>/api/widget/note` with the header `Authorization: Bearer <token>` and the JSON body `{"title": "optional", "body": "text"}`. The note's title is the key's name, plus ` — <title>` when a title is given. An empty body is refused (400), and the body is capped at 100,000 characters (413). The reply is `{"session_id": …}`. `GET <url>/api/widget/info` with the same header shows which project and document the key is bound to.
