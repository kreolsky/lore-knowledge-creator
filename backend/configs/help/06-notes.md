# Notes

A note is a discussion thread attached to a document, optionally to a fragment of its text. Comments and replies live in the thread.

## Creating a note

- Select text and press ⌘/Ctrl + M or "Note" on the selection toolbar. The fragment is highlighted in yellow with a dashed underline.
- "+" in the "Notes" tab creates a note not tied to text.

## Notes and the text

A note is not part of the text: it lies over the document like a layer and is anchored to its fragment. The highlight moves with the fragment while the document is edited, including by other people, the agent, a snapshot restore or a re-upload. If the fragment is deleted, the note stays in the list with its quote and the "fragment deleted" mark; undo does not reattach it.

## Working with notes

- All of a document's notes are in the "Notes" tab; an open note shows the thread. A long thread opens on its latest messages; "Load older messages" shows the earlier ones.
- Hovering a highlighted fragment shows its note.
- The "Commentator" role cannot edit documents but can write notes — the way to collect remarks from readers.

## Notes from guests

When comments are allowed on a public link (see [Access and publishing](help:access)), anyone holding the link selects text and leaves a note under a name of their choice. A guest sees every note on the document with its author's name, can reply, and can delete their own message until someone replies to it. A guest message is capped at 2000 characters. Members see guests' notes in the "Notes" tab like any other, and a guest sees members' notes and replies within a second.

## Notes that arrive by themselves

- **System notes.** A failed pipeline or agent action posts its details as a new system note of the document.
- **Notes from a widget key.** A text sent over the widget-key API (`POST /api/widget/note`) lands as a note of the key's document, titled with the key's name, and is marked unread for the key's owner (see [Interface](help:interface)). The recording apps do not send notes yet; they upload files.

## Sending a note through a widget key

The copied widget key is base64 of `{"url": …, "token": …}`. Decode it and send `POST <url>/api/widget/note` with the header `Authorization: Bearer <token>` and the JSON body `{"title": "optional", "body": "text"}`. The note's title is the key's name, plus ` — <title>` when a title is given. An empty body is refused (400), and the body is capped at 100,000 characters (413). The reply is `{"session_id": …}`. `GET <url>/api/widget/info` with the same header shows which project and document the key is bound to.
