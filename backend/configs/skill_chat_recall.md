---
name: chat-recall
description: Use when the user refers to what was said in another of their chats in this project, or asks to list, read or analyse one of their chats. «мы обсуждали», «я говорил в другом чате», «что мы тогда решили», «разбери чат», «список чатов»; we discussed, in the other chat. Saving facts from this conversation is the memory skill.
tools:
  - search_my_chats
  - list_my_chats
  - read_my_chat
  - view_chat_image
---

# Chat recall

The user's other AI chats of this project — their own only, never notes, never this
conversation.

## When

The user points at an earlier conversation ("we discussed", "what did we decide
about …"), or the answer plainly depends on something they said before that this
conversation does not hold. Search before asking them to repeat themselves.

## Recall a fragment

1. `search_my_chats` with `query` in the words it was likely said in.
2. A snippet recognises the hit; it is not a quote. Read the turn with
   `search_my_chats` + the hit's `message_id` before quoting.
3. Tell the user which chat and when ("in «<chat title>» on <date> you said …").

## Images

A message marked `images: N` has images. The user asks what one shows, to show it,
or to save it → `view_chat_image` FIRST, even when the chat already describes it:
that text is what someone said about the image, the image is the source. The call's
plate shows the image to the user, so your reply names it in words (which image,
from where) and carries no image markup. To save it: `import_file` with that
`message_id` and `index` as `attachment_index`.

## Whole chat

To list, read or analyse a chat: `list_my_chats` (with `with_topics: true` when the
user asks what their chats were about), then `read_my_chat` with `topics_only: true`
— the topics are the map; jump with `from_turn`, or read from turn 0 following
`next_turn`. A topic is a summary: quote and decide only from turns you read. One
exception: a topic's `marks` carry the user's own `quote` — rely on it as said then.
A topic without a mark says nothing about what they like.
`topics: null` means no table of contents yet — read the chat.

A hidden (`unindexed`) chat is invisible to these tools — if the user asks for one,
ask them to turn hiding off first.

## What a hit is

A hit is what was SAID then — a proposal, a guess, a decision that may since have
changed — not an established fact. Project knowledge lives in documents and memory
(`search_materials`); when the two disagree, say so and prefer the project's material.

A result with `indexed: "N of M"` covered only part of the chats. A miss there is
"not found yet", never "it was never said".
