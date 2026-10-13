---
name: chat-recall
description: Use when the user refers to something said in another of their chats in this project — recall it instead of asking them to repeat it. Вспомни, мы обсуждали, я говорил, в другом чате, в прошлом разговоре, что мы решили тогда. Loads search over the user's own other chats and its tool.
tools:
  - search_my_chats
---

# Chat recall

The user's other AI chats of this project are searchable with `search_my_chats` —
their own chats only, never another person's, never notes, never this conversation.

## When

The user points at an earlier conversation ("we discussed", "as I said in the other
chat", "what did we decide about …"), or the answer plainly depends on something
they said before and this conversation does not hold it. Search before asking them
to repeat themselves.

## How

1. Call `search_my_chats` with `query` in the words the thing was likely said in.
   Each hit names its chat, its date, who said it (`role`) and — for an assistant
   reply — the `question` that prompted it.
2. A snippet is enough to recognise the hit, not to quote it. To read what was
   actually said, call `search_my_chats` with the hit's `message_id`: it returns
   that turn plus one turn above and below. Follow `earlier` / `later` to move
   through the chat, and `next_offset` to finish a long message.
3. Tell the user which chat and when ("in «<chat title>» on <date> you said …").

## What a hit is

A chat hit is what was SAID then — a proposal, a guess, a decision that may since
have changed. It is not an established project fact: established knowledge lives in
the project's documents and memory, and `search_materials` is the tool for it. When
the two disagree, say so and prefer the project's material.

A result with `indexed: "N of M"` covered only part of the chats — recent messages
may not be searchable yet. A miss there is "not found yet", never "it was never
said".
