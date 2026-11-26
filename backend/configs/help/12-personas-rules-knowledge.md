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

A skill is, for a persona, in one of three states: **loaded from the first message** (named in `skills`, and the default skills unless `preload_defaults: false`); **visible, loaded on demand** (the agent sees it and loads it when a request needs it); or **hidden** (kept out by `allow` or named in `deny`: the agent does not see it, cannot load it, and its tools are refused).

- `skills` — skills loaded for this persona from the first message, without waiting for the agent to load them itself. Use the skill names from the Skills folder or the built-in skills, or a prefix ending in `*` (`main-*`). A name that matches no enabled skill, or one this persona hides with `allow` or `deny`, is skipped.

- The default skills are the four `main-*` skills — everything the agent does with documents ([Skills](help:skills)). They are loaded from the first message in every chat by default, in «Default» as in any persona that says nothing about them.

- `preload_defaults: false` — the default skills are not loaded from the first message. The agent still sees them and loads one itself when a request needs it: a persona that answers questions starts with reading only and picks up editing when you say "fix it". List in `skills` what it does start with. Without this key, or with any other value, they are loaded. A helper agent the agent starts for a subtask gets only the tools the agent has active at that moment: until a reading skill is loaded, the helper cannot open documents either — keep `main-read` in `skills` if the persona delegates research.

- `allow` — the only skills this persona may use, as a list: `[main-*, web-search]`. A skill outside the list is hidden. Without this key every skill is allowed.

- `deny` — skills hidden from this persona, as a list: `[main-edit, tables]`. Hidden means the same as outside `allow`: not in the agent's list, not loadable, its tools refused. An [external MCP server](help:chat-agent) that connects when the agent needs it is such a skill too, named `mcp-<server name>`: denied, it stays unconnected. Two tools are not skills and stay whatever the lists say: loading a skill, and the plain `web_search`.

- A ban always beats a permission: `deny` wins over `allow` and over `skills`, whatever lists they are in. With `allow: [main-*]` and `deny: [main-files]` the persona does not see `main-files`.

- `base_prompt: false` — the persona's own text is the agent's whole role. The default description of the agent (Lore's home agent) and its rules about when to answer and when to change a document are not added; the lines every agent keeps — whose rights it acts with, and what a landed change is — stay. Without this key, or with any other value, the default text is kept.

- `chat_memory_budget` — turns on conversation memory for this persona (see below). A positive number is the size of the memory in tokens, for example `chat_memory_budget: 1500`. Without this key memory is off. The administrator sets a ceiling (Settings → Agent, 32000 tokens by default); a larger number is reduced to it. How much fits: the budget is counted as characters ÷ 4 × 0.85 — about 1.5 tokens per Russian word and 1.3 per English word; a chat with three topics takes about 200 tokens. A misspelled value (for example `"2000"` in quotes, or a negative number) turns memory off, and the chat says so.

- `chat_memory_order` — the order of chats in conversation memory: `recent` (the newest first, the default) or `document` (chats under the current document first, then the newest). A misspelled value falls back to `recent`, and each turn shows a warning.

- `chat_recall_budget` — turns on auto-recall for this persona (see below): the size of the recalled excerpts in tokens, for example `chat_recall_budget: 2000`. It works only together with `chat_memory_budget`; without it auto-recall is off. The administrator's memory ceiling applies here too. A misspelled value turns auto-recall off; in both cases the chat says why.

### Example: a persona that remembers your past chats

Open the persona document in the Personas folder and put the settings block at the very top, before its text:

```yaml
---
chat_memory_budget: 16000
skills: [web-search]
---
You are a careful research assistant...
```

- `chat_memory_budget: 16000` — each new chat with this persona receives up to 16000 tokens of your past chats with it: about 80 chats of three topics each. Older chats do not fit, and the agent finds them by search when you refer to them. Write the number without quotes.
- `skills: [web-search]` — web search is loaded from the first message, alongside memory.
- The chat skill (chat-recall) is not listed: with memory on it is loaded from the first message by itself, and the agent can search, list and read your other chats right away. A persona without `chat_memory_budget` lists `chat-recall` in `skills` to get the same from the first message; otherwise the agent loads it itself when you refer to an earlier conversation. Either way the skill must not be switched off (see [Skills](help:skills)) or hidden by the persona's own `allow` / `deny` — in both cases conversation memory is off, and the chat says why.
- The settings take effect at the next message; nothing needs restarting. A chat that has already received memory keeps it; a chat that has not gets it at its next turn.

If the block cannot be read (for example, a typo in its syntax), the persona works as before with its whole text, and the settings are ignored; when the block mentions `chat_memory_budget`, the chat says that memory is off.

### Examples: personas and skills

Open the persona document in the Personas folder and put the settings block at the very top, before its text.

**A persona that answers first and edits on request.**

```yaml
---
preload_defaults: false
skills: [main-read]
---
You are a researcher of this project. Answer from its documents and cite them.
```

- `preload_defaults: false` — the default skills are not loaded from the first message, so the editing instructions stay out of the conversation until they are needed.
- `skills: [main-read]` — reading the project is loaded from the first message. Nothing is hidden: when you say "fix the typo in that document", the agent loads `main-edit` itself and makes the change.

**A closed persona.**

```yaml
---
allow: [main-*]
---
You are an editor of this project. You work with its documents only.
```

- `allow: [main-*]` — only the skills whose name starts with `main-` exist for this persona: no web research, no tables, no code console, no memory. If the agent tries one of their tools, the call is refused. Keep `main-read` in the list: without it the agent edits documents it never read — `edit_document` needs the exact text copied from a fresh read.

**A persona that never edits.**

```yaml
---
deny: [main-edit]
---
You are a proofreader. Point out what to fix and quote the corrected text; never change a document.
```

- `deny: [main-edit]` — the editing skill is hidden: not in the agent's list, not loadable, and `create_document`, `edit_document`, `append_to_document` and `rename_document` are refused. The persona's text asks for the same, and `deny` makes it hold. `main-read` stays allowed, so the agent still opens what it talks about.

### Conversation memory

Conversation memory (память разговоров) lets a persona start each new chat knowing what it talked about with you before. It is not the project memory of facts described in "Memory": nothing is extracted into documents, and it covers only your own chats with this persona.

- After a chat with the persona has gone on for a while, or has paused for about three minutes, its topics are noted in the background: a short title, one or two sentences, the kind of talk (bug, idea, personal…), how it ended, and what you said you feel about something — only together with your own words, quoted. Topics are noted for every chat with the persona; when memory is turned on, older chats catch up in the background.
- When you start a new chat with the persona, the agent receives the topics of your chats with it: the newest first, or — with `chat_memory_order: document` — chats under the current document first. Chats go in whole, newest first, until the budget is used up; the rest the agent finds by search when you refer to them.
- The chat shows that memory is on: the persona list shows a brain instead of the scroll icon when the selected persona has memory, and a "Persona memory" row before the message that brought it lists those chats. If memory could not be turned on, the icon turns red and its tooltip says why.
- A persona chosen mid-chat brings its memory at the next turn. Switching to another persona mid-chat swaps the memory: from the next turn the agent no longer has the previous persona's memory and receives the new persona's instead — or a note that this persona has none. Switching back brings the first memory back.
- On request the agent can look up the topics of any of your chats with it.
- The agent sees only topics. A quoted attitude it may rely on as said then; to answer anything else in detail it opens the chat itself, and it can tell you which chat and when. The very latest chat may not be noted yet; the agent reads it when you ask about your last conversation.
- With `chat_recall_budget` the persona also remembers on its own. Among the first five messages of a new chat, the ones close in subject to something you discussed before bring the original exchanges in by themselves: your words and the agent's answers as said then, at most two exchanges per topic, grouped by topic — by chat only for an exchange that has no topic. Short messages («thanks», «ok») are not compared. The agent is told that a decision quoted there may have changed since, and names the chat and the date when it relies on one. Once a chat has recalled something it does not do so again; a continuation after a long chat starts over. A "Recalled" row before the message that brought them lists those chats, and each opens that chat. If the recall could not be made, the chat says so and the answer is given without it.
- Only your own chats count, never another member's. A chat belongs to the memory of the persona selected in it: switching persona moves the whole conversation (its branches and continuations) to the new persona's memory, and «Default» takes it out of conversation memory. In the chat list a brain before the persona name marks a chat that is in a persona's memory. Deleted chats and chats marked "Unindexed" are left out.
- If the administrator disables the `chat-recall` skill, conversation memory is off as well.

## Rules

What the agent needs to know about this project and how work is done in it: conventions, your preferences, corrections to the agent's habits — and what is true in this project: the subject, the people, the decisions. The agent receives the rules in every conversation, so keep them short; large volumes of facts are better kept in [memory](help:memory). You can ask: "remember that we capitalize mechanic names" — and the agent adds a rule.

## Skills

The project's skills folder. See [Skills](help:skills).

Rules can be split by topic across child documents. The agent receives the whole section.
