# Voice notes

A recording is transcribed to text automatically and saved as a reference. Recording, audio upload and voice input need an account with the voice tier or higher (see [Access and publishing](help:access)); without it the record and microphone buttons are hidden.

## A voice note for a document

The record button in the header, or ⌘/Ctrl + D when the cursor is not in the text. The recording becomes a reference, and its transcript appears in the "References" tab.

Where recordings land is set in project settings, "Voice Recording" section:

- **Current document** (default) — the open document.
- **Specific document** — one chosen document regardless of what is open; an "inbox" to sort later.

If the connection drops during recording, the recording is kept in the browser and sent the next time the project is opened.

## Voice input into text

With the cursor in the document's text, ⌘/Ctrl + D inserts the transcript at the cursor. The chat and notes have a microphone button next to the input field; Escape discards the take without transcribing it.

## Apps for Android and macOS

Voice recorders that can also record system audio (calls). Recordings from an app land as references of the document its key belongs to. The Android APK, when the administrator has published it, is downloadable from `/downloads/` on the Lore site.

Lore Voice for Android:

- holds several keys, one per document, shown as tabs on the recording screen; recording also works without a key, and the files wait on the phone;
- records the microphone, the system audio, or both at once;
- uploads through a queue that retries on failure; long uploads continue in the background;
- keeps a history of recordings: swipe to delete (with undo), play back, open a recording's card;
- shows the transcript once the server has it;
- in Settings: the app version and an "Upload journal" with the server's answer to every upload, which can be copied when something fails.

Connecting an app:

1. Open the target document.
2. "Access" tab → "API access" → create a key with "widget" ticked.
3. Copy the key (it is shown once) into the app.

Arrivals from the app are marked unread for the key's owner (see [Interface](help:interface)).
