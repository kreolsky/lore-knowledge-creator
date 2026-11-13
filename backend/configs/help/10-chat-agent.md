# Chat and the agent

The chat is an agent with tools. It reads, searches, creates, edits, moves and renames documents, builds tables and [HTML pages](help:html-pages), and works with a document's [snapshots](help:history): lists them, reads and compares old versions, restores one. Through [skills](help:skills) it also searches the web, runs deep research, generates pictures, runs code in a sandbox console, works with [memory](help:memory) and pipeline configs, recalls your other chats and calls outside services with your [personal keys](help:profile).

The agent acts on your behalf: it sees and changes only what you can see and change. The chat needs an account tier with AI (see [Access and publishing](help:access)); without it the Chat tab is hidden.

## Chats are project-wide and personal

All your chats in a project are visible from any document; other members never see them, and you never see theirs. Every chat has a parent — the document or reference it was started from — which affects sorting and where facts saved from the chat belong (see [Memory](help:memory)). The parent is changed with the change-parent icon in the chat header, for all branches of the chat at once. A chat can be moved to any document or to project level, never onto a reference; a reference's chat moved this way becomes an ordinary document chat.

- **Sort**: by recent activity (default) or by proximity to the open document (its recent chats first, then chats of nearby documents in the tree).
- **Find a chat**: the "Search" tab in "Chats" mode, by title or by content (see [Search](help:search)).
- Rename (double click), export to Markdown or JSON, delete.
- A chat where the agent is working right now is marked with a live indicator in the list.
- **Analyse in a new chat** (chat header) opens a new chat with a request to read this chat whole; the agent can list your chats and read any of them in full. Not available for an unindexed chat.

## Context

With every message the agent receives links (not content) to the open documents and to the documents they link to directly, and reads what it needs itself. "Add content" adds or removes documents and references by hand; switch off what is not needed if the agent reads too much.

The indicator next to the input shows how full the context window is. On overflow the older part of the conversation is compressed; if that is not enough, start a new chat.

## Model and reasoning

Model and reasoning mode are picked next to the input. "default" leaves the reasoning depth to the model. The administrator decides which models each user can pick and in what order the picker lists them; a model that is not available to you is refused and the picker opens.

## Confirming the agent's edits

- **"Auto" unchecked** (default) — every change is shown first: allow once, allow for this chat, or reject. The card shows the proposed change: "before / after" for text and cell edits, the call's arguments for other actions. A rejection with an explanation makes the agent try another way.
- **"Auto" checked** — changes apply at once. A snapshot is taken before every agent edit, so it can be rolled back (see [Snapshots and history](help:history)).

Some actions ask every time, even with "Auto" checked: a request with your personal key that sends data (anything but reading), and a call to an external MCP server's tool that the server does not mark as read-only — unless the administrator (or your group's moderator) set that server to "Run without confirmation"; then all its tools run at once.

## Branches

- **Edit a message** — the pencil on your message, then "Save" (✓): the conversation continues from the edited message as a new branch, the old one is kept. Escape cancels the edit.
- **Continue from before a message** — the trash icon on your message (press twice): hides that message and everything below; your next message starts a new branch from that point. Nothing is deleted. Use it when a message cannot be fixed by editing, for example an attached image that now fails.
- Branches are switched with the "◄ 1/2 ►" arrows on the message where they split.
- The first message of a chat cannot be edited or rewound; start a new chat instead.

## Sending while the agent works

A message sent while the agent is busy is queued: it shows as a chip with ✕ (removes it) and runs as its own turn after the ones before it. The queue survives a page reload and is visible in every tab. Images can be queued too.

## Chat privacy: "Unindexed"

The shield button in the chat header marks the chat unindexed (the chat list shows it tinted). An unindexed chat is not found by the agent's recall from your other chats and never gets into a [digest](help:pipelines). The flag covers all branches of the chat and can be switched off any time.

To hide all chats of a document and its subtree at once, use "Hide this document's chats" in the "Access" tab (see [Access and publishing](help:access)): outside the subtree such chats disappear from the chat list and from the agent's search. A chat bound to a page locked with your PIN stays hidden until the PIN is entered (see [Screen and page lock](help:pin-lock)).

## Other actions

- **Quote in chat** (⌘/Ctrl + J, or the button on the formatting toolbar) — the selected text goes into the chat input as a quote, followed by a link to that passage of the document. Add your request and send it. A click on the link, in the chat or in a document, opens the document with the passage selected.
- **Clarify** — select part of the agent's answer and ask a follow-up about it.
- **Continue** — an interrupted turn (step limit, error, lost connection) leaves a card with the reason and a "Continue" button. A reply cut off by an error stays on screen, also after a reload.
- **Create document from response** — the answer becomes a new document.
- **Voice input** — the microphone next to the input.
- **Pictures** — drag or paste them into the chat, if the model can see images. Each picture is compressed once when attached, and that version is exactly what the model receives: the chip shows its size ("…, compressed from …"), and a click on the chip opens it at full size, so you can check whether small text is still readable. Screenshots need no shrinking by hand. A generated picture opens in a preview with "Download" and "Delete image"; drag it into a document to insert it. The agent can also look at pictures from your other chats and save any chat picture as a reference.
- **Files** — any other file (up to 100 MB, not RAR) dropped on the chat uploads into your sandbox workspace, and the agent is told its path and works on it in the console. This needs full access to the project and a configured sandbox; in a note's chat only pictures can be attached. A file that cannot be attached shows a message.
- **System prompt** — the persona for this chat (see [Personas, rules and knowledge](help:personas-rules-knowledge)).
- **Prompt rail** — the ticks on the right edge of the chat, one per your message. Hovering (or tapping) opens a list of your prompts with the start of each answer; a click jumps to that prompt.
- **Scroll to bottom** — the arrow button that appears when the chat is scrolled up.
- **Sound** — a sound when the agent finishes a turn or asks you to confirm a change, handy while you work in another window. Turn it on per project in the "Settings" tab, "Interface" section; pick or upload the sound in [Profile settings](help:profile), "Sound" section. Off by default.

## External MCP servers

The administrator (or your group's moderator) can connect outside MCP servers; their tools then appear in your chat, named after the server. Where a server needs your own key, paste it in [Profile settings](help:profile), "MCP" section. The agent prefers Lore's own tools for work on the project and uses a server's tools when the task is for that service or you name it.
