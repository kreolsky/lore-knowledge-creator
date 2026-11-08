---
name: chat-recall
description: Use when the user refers to something said in another of their chats in this project — recall it instead of asking them to repeat it — or asks to list, read or analyse one of their chats whole, or asks to SAVE knowledge from THIS conversation into project memory. Вспомни, мы обсуждали, я говорил, в другом чате, в прошлом разговоре, что мы решили тогда, разбери чат, проанализируй сессию, список чатов, прочитай чат целиком, сохрани в знания, сохрани это в знания, извлеки из разговора, запомни это, запомни в память, привяжи факт к документу, save facts from this chat. Loads search over the user's own other chats and its tools, and the moderated capture of facts from this conversation.
tools:
  - search_my_chats
  - list_my_chats
  - read_my_chat
  - view_chat_image
  - save_chat_facts
  - set_fact_anchor
  - get_memory_facts
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
3. A served message marked `images: N` has N attached images. The user asks
   what an image shows, to show it, or to save it → call `view_chat_image`
   first (that message's `message_id`, the image's 0-based `index`), even when
   the chat already holds a description of it: that text is what someone said
   about the image, the image is the source. Then answer from what you saw.
   The user sees the image in the call's plate, so the plate IS how you show
   it: your reply says in words which image it is and where it came from, and
   carries no image markup. To
   save it, call `import_file` with the `message_id` and `index` (as
   `attachment_index`) of the image you just viewed. A linked image answers
   `unavailable`.
4. Tell the user which chat and when ("in «<chat title>» on <date> you said …").

## Whole chat

To list, read or analyse a whole chat (not just recall a fragment):

1. `list_my_chats` — every other chat of this project (the user's own only),
   newest activity first. Narrow by `title_contains` or `under_document_id`,
   follow `next_offset` while it is present.
2. Pick the chat by title, document or date, then `read_my_chat` from turn 0.
   Follow `next_turn` to the end — no turn is lost or repeated.
3. A message marked `truncated` continues in `search_my_chats`: call it with
   that `message_id` and `offset` = the `next_offset` shown, until the message
   is done. Never count characters yourself.

A hidden (`unindexed`) chat is not visible to these tools — if the user asks
for one, ask them to turn hiding off first.

## What a hit is

A chat hit is what was SAID then — a proposal, a guess, a decision that may since
have changed. It is not an established project fact: established knowledge lives in
the project's documents and memory, and `search_materials` is the tool for it. When
the two disagree, say so and prefer the project's material.

A result with `indexed: "N of M"` covered only part of the chats — recent messages
may not be searchable yet. A miss there is "not found yet", never "it was never
said".

## Saving facts from this chat

When the user asks to keep what THIS conversation established («сохрани это в
знания», «запомни это», "save this"), distil it into memory facts with
`save_chat_facts` — the same facts `search_materials` serves later. This is the
ONLY path for knowledge from the current conversation; memory-consolidation
walks a document's attached references, not chats.

Distil BEFORE saving: read back what was said (this conversation is in your
context), pick out what is worth keeping, and check `search_materials` for the
same knowledge already stored — a restatement is a `merge`, not a new fact.

- One fact is ONE assertion. Its `title` reads as a name — `Субъект: краткая
  суть` — and its `text` body IS the fact: plain prose, stored verbatim. Keep
  every qualifier that bounds what is claimed (the condition, the period, the
  dose); a claim stripped of its condition is a false universal.
- **A fact never refers to the material, the speaker or this session.** The
  server stamps this chat as the fact's source — repeating it in the prose
  costs the opening of every body. Write so it reads true to someone who has
  neither this chat nor the documents.
- Verdicts, one per fact, exactly as `apply_memory_verdicts` takes them:
  `new` (title + text), `merge` (`fact_id`, optional `absorb_id`, `text` to
  reword), `supersede` (`fact_id` + `reason` + `text` — the target is the
  FIELD, never an id inside fact text; on a contradiction, show both facts and
  let the user decide), `skip` (`reason`).
- No `reference_id`, no run, no loop — one call, one batch. The result names
  every fact written (`created` / `touched`); take ids from there. In confirm
  mode one batch produces ONE card: a rejection with text is the user's
  moderation — drop or fix the named verdicts and re-emit the corrected batch
  ONLY. The `rejected` list names what to fix; a `new` whose reason names a
  stored fact is the duplicate gate — send a `merge` against it instead.
- After saving, the facts follow THIS chat's parent document in narrowed
  searches. To place one elsewhere, call `set_fact_anchor(fact_id,
  document_ids)` — the fact then belongs to those documents' subtrees and
  stops following the chat; `document_ids: []` clears that and the fact
  follows the chat again. A fact's current anchors are served by
  `get_memory_facts`.
- The fact body is project-visible — saving is publishing. Say what you saved
  and where it is attached.
