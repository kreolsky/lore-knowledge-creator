# Personas and rules

How the agent behaves in a project is set by documents in the system section of the tree, "Agent System". These are ordinary documents: you can read and edit them, and the agent can add to them too.

## Personas

A persona is an additional system prompt that gives the agent a role and a manner: for example, "critic", "style editor", "game designer".

- Create a document inside the Personas folder. It becomes a persona right away.
- Choose the persona in the chat, in the persona list at the left of the row above the chat input. By default no persona is selected («Default»). A brain before a name marks a persona with conversation memory.

### Persona settings

A persona can start with a settings block between two `---` lines. The agent does not see this block, only the text below it.

```yaml
---
skills: [chat-recall, web-search]
---
You are a careful research assistant...
```

- `skills` — skills loaded for this persona from the first message, without waiting for the agent to load them itself. Use the skill names from the Skills folder or the built-in skills. A name that does not match an enabled skill is skipped.

- `chat_memory_budget` — turns on conversation memory for this persona (see below). A positive number is the size of the memory in tokens, for example `chat_memory_budget: 1500`. Without this key memory is off. The administrator sets a ceiling (Settings → Agent, 32000 tokens by default); a larger number is reduced to it. How much fits: the budget is counted as characters ÷ 4 × 0.85 — about 1.5 tokens per Russian word and 1.3 per English word; a chat with three topics takes about 100 tokens. A misspelled value (for example `"2000"` in quotes, or a negative number) turns memory off, and the chat says so.

- `chat_memory_order` — the order of chats in conversation memory: `recent` (the newest first, the default) or `document` (chats under the current document first, then the newest). A misspelled value falls back to `recent`, and each turn shows a warning.

### Example: a persona that remembers your past chats

Open the persona document in the Personas folder and put the settings block at the very top, before its text:

```yaml
---
chat_memory_budget: 8000
skills: [web-search]
---
You are a careful research assistant...
```

- `chat_memory_budget: 8000` — each new chat with this persona receives up to 8000 tokens of your past chats with it: about 80 chats of three topics each. Older chats do not fit, and the agent finds them by search when you refer to them. Write the number without quotes.
- `skills: [web-search]` — web search is loaded from the first message, alongside memory.
- The chat skill (chat-recall) is not listed: with memory on it is loaded from the first message by itself, and the agent can search, list and read your other chats right away. A persona without `chat_memory_budget` lists `chat-recall` in `skills` to get the same from the first message; otherwise the agent loads it itself when you refer to an earlier conversation. Either way the skill must not be switched off (see [Skills](help:skills)).
- The settings take effect at the next message; nothing needs restarting. A chat that has already received memory keeps it; a chat that has not gets it at its next turn.

If the block cannot be read (for example, a typo in its syntax), the persona works as before with its whole text, and the settings are ignored; when the block mentions `chat_memory_budget`, the chat says that memory is off.

### Conversation memory

Conversation memory (память разговоров) lets a persona start each new chat knowing what it talked about with you before. It is not the project memory of facts described in "Memory": nothing is extracted into documents, and it covers only your own chats with this persona.

- After a chat with the persona has gone on for a while, or has paused for about three minutes, its topics are noted in the background: a short title and one sentence per topic. Topics are noted for every chat with the persona; when memory is turned on, older chats catch up in the background.
- When you start a new chat with the persona, the agent receives the topics of your chats with it: the newest first, or — with `chat_memory_order: document` — chats under the current document first. Chats go in whole, newest first, until the budget is used up; the rest the agent finds by search when you refer to them.
- The chat shows that memory is on: the persona list shows a brain instead of the scroll icon when the selected persona has memory, and a "Persona memory" row before the message that brought it lists those chats. If memory could not be turned on, the icon turns red and its tooltip says why.
- A persona chosen mid-chat brings its memory at the next turn. Switching to another persona mid-chat swaps the memory: from the next turn the agent no longer has the previous persona's memory and receives the new persona's instead — or a note that this persona has none. Switching back brings the first memory back.
- On request the agent can look up the topics of any of your chats with it.
- The agent sees only topics. To answer details it opens the chat itself, and it can tell you which chat and when. The very latest chat may not be noted yet; the agent reads it when you ask about your last conversation.
- Only your own chats count, never another member's. A chat belongs to the memory of the persona selected in it: switching persona moves the whole conversation (its branches and continuations) to the new persona's memory, and «Default» takes it out of conversation memory. In the chat list a brain before the persona name marks a chat that is in a persona's memory. Deleted chats and chats marked "Unindexed" are left out.
- If the administrator disables the `chat-recall` skill, conversation memory is off as well.

## Rules

What the agent needs to know about this project and how work is done in it: conventions, your preferences, corrections to the agent's habits — and what is true in this project: the subject, the people, the decisions. The agent receives the rules in every conversation, so keep them short; large volumes of facts are better kept in [memory](help:memory). You can ask: "remember that we capitalize mechanic names" — and the agent adds a rule.

## Skills

The project's skills folder. See [Skills](help:skills).

Rules can be split by topic across child documents. The agent receives the whole section.
