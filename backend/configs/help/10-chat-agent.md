# Chat and the agent

The chat is an agent with tools. It knows your project and can work with documents: read, search, create, edit, move, rename, build tables. Beyond that, it can search the internet, generate pictures, and work with skills and project memory.

The agent acts on your behalf: it sees and changes only what you can see and change.

## Chats are project-wide

All your chats in a project are visible from any document. Chats are personal: you don't see other people's chats, and they don't see yours. Formally every chat has a parent — the document or reference it was started from — but this restricts nothing. A chat's parent can be changed.

- **Search chats by title** — a filter above the list.
- **Sort by recent activity** — the newest chat on top. This is the default.
- **Sort by proximity to this document** — recent chats of the open document on top, then chats of documents near it in the tree.

A chat can be renamed with a double click, exported to Markdown or JSON, and deleted.

## Context

When you write in the chat, the agent automatically receives links to the open documents and to the documents they link to directly. Links are passed, not content: the agent reads what it needs itself if the question concerns those documents.

The "Add content" button lets you add or remove documents and references by hand. If the agent reads too much, switch off the context you don't need.

## Model and reasoning mode

The model and the reasoning mode are chosen next to the input field. "default" leaves the choice to the model. Deeper reasoning gives more considered answers but is slower.

## The agent's edits

- **"Auto" unchecked** (the default) — the agent shows every change first, and you decide: allow once, allow in this chat, or reject. If you reject with an explanation, the agent tries a different way.
- **"Auto" checked** — edits are applied right away. Before each of the agent's edits Lore takes a snapshot, so it can be rolled back (see [Snapshots and history](help:history)).

## Handy moves

- **Work with selection** (⌘/Ctrl + J) — select a fragment in the document, and the agent works on exactly that.
- **Clarify** — select part of the agent's answer and ask a follow-up question.
- **Edit your message** — the pencil on your message. After saving, the conversation continues as a new branch and the old one is kept; switch between branches with the arrows on the message.
- **Continue** — if the agent's turn was interrupted (step limit, error, lost connection), a card with the reason and a "Continue" button appears in the chat.
- **Create document from response** — the agent's answer becomes a new document.
- **Voice input** — the microphone button next to the input field.
- **Pictures** — drag them into the chat if the model can see images.
- **System prompt** — choose a persona for this chat (see [Personas, rules and knowledge](help:personas-rules-knowledge)).

The indicator next to the field shows how full the context window is. When it overflows, the agent compresses the older part of the conversation. If there is still not enough room, start a new chat.
