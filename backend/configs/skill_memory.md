---
name: memory
description: Use when the user asks to remember something in project memory, or to consolidate, clean up or verify it — from this conversation or from a document's references — or to attach a fact to a document. «запомни это», «добавь в память», «сохрани обсуждение», «консолидируй память», «почисти память», «привяжи факт к документу»; remember this, consolidate memory, attach this fact to a document.
tools:
  - consolidate_memory
  - get_memory_facts
  - apply_memory_verdicts
  - next_reference
  - get_fact_history
  - reopen_consolidation
  - save_chat_facts
  - set_fact_anchor
---

# Memory

Project memory is a set of **facts**: one document per fact, its body IS the fact, the
same facts `search_materials` serves later. Two ways in, one set of rules for writing a
fact and for verdicts (below).

## Which way

Route by the OBJECT of the request, never its verb — «консолидируй», «запомни», «добавь»
name either way.

- **Object is this conversation** («это», «обсуждение», «что мы обсудили», «что
  решили», «итоги», "this chat") ⇒ `save_chat_facts`. «Сконсолидируй то, что мы
  обсудили» is a save from this conversation, not a run.
- **Object is material** (a document, its references, files, a transcript) ⇒ the run
  over that document's references.
- **No object, and this chat holds a substantive discussion** ⇒ before any tool call ask
  ONE question: this conversation, the references of «<current document>» (as the
  `# Current document` section names it), or both.
- **No object, no discussion** ⇒ the run, on the current document.
- **Both** ⇒ `save_chat_facts` first, then the run, so the run merges against them.
- **«Почисти память» with no document** ⇒ there is no standalone cleanup of stored
  facts — it happens inside a run; offer a run over the current document.

## From this conversation

Read back what was said, pick what is worth keeping, and check `search_materials` for
the same fact already stored (a restatement is a `merge`). Then ONE `save_chat_facts`
call with the verdicts — no `reference_id`, no loop; the server stamps this chat as the
source.

- In confirm mode a rejection with text is the user's moderation: fix or drop the named
  verdicts and re-emit only those.
- Saved facts follow this chat's parent document in narrowed searches.
  `set_fact_anchor(fact_id, document_ids)` places a fact under other documents instead;
  `document_ids: []` makes it follow the chat again.
- Saving is publishing to the project. Say what you saved and where it is attached.

## From a document's references

1. **Target.** The document the user names (a reference id re-anchors onto its host's
   whole stack); none named ⇒ the current document, no second question. Breadth is the
   target's whole subtree: `get_project_structure` with the target as `start_id`, then
   one full run per listed document, each closed to `complete` before the next. "This
   document only" narrows it to one run. Say which document you are consolidating.
2. **Loop.** `consolidate_memory(target_doc_id)` → consolidate THIS window →
   `apply_memory_verdicts(reference_id, verdicts)` →
   `next_reference(completed_reference_id, completed_window_index)` → repeat until
   `complete: true`. Do not pass `run_id`. Finish a window before stamping it — its
   material then leaves your context. A fact straddling a window seam meets its twin as
   a merge candidate in the next window: merge it. `waiting: true` ⇒ stop and say the
   next reference is still transcribing.
3. **Exhaust each window.** Extract every fact, apply, then re-read for what the first
   pass dropped (asides, dates, the second item in a sentence about the first, a
   relation in passing); stop only when a re-read finds nothing new. Use as many batches
   as needed, each small enough to emit in one call — an oversized batch truncates
   mid-JSON and never succeeds.

**Redo.** Only on an explicit request to re-consolidate (new focus, another model):
`reopen_consolidation(target_doc_id)` clears the done-stamps, touches no fact; then run
again. Existing facts come back as candidates, so overlap lands as merge/supersede.

**Close with a report**: the delta (created / touched, against `stats_at_start`, not a
fresh census); `stats.facts` live and `stats.retired`; `reference_shape` per reference
— say it plainly, a uniform small count per reference is the quota this run must avoid;
where it ended (`progress`); unreadable references (`unusable_skipped`, `waiting`);
what you skipped and why; any unanswered question, verbatim.

## Writing a fact

ONE assertion. `title` reads as a name — `Субъект: краткая суть` (`Аспирин:
антиагрегант при риске нарушения функции плаценты`); `text` is the fact in plain prose,
nothing else (no links). A body promoted to a title (`Роль тромбоцитов при
имплантации`) is unmatchable, and every later run then forks a new layer — this rule
matters most.

Write in the project's language and the source's wording, readable by someone without
the material, this chat or this session. Keep every qualifier that bounds the claim —
condition, period, population, dose; drop everything about where you read it: **a fact
never mentions the material, the speaker or this session** (the server stamps the
source). Keep dates, in the wording.

## Verdicts — one per fact

Same shapes for `save_chat_facts` and `apply_memory_verdicts`:

- **`new`** — `title` + `text`.
- **`merge`** — the same stored fact again: `fact_id` (+ `text` to reword, + `absorb_id`
  to fold a second stored fact in). A merge that adds no new source is dedup, not confirmation.
- **`supersede`** — corrects a stored fact in place, history kept: `fact_id` + `reason`
  + `text`. The target is the `fact_id` field only. On a contradiction **ask, don't
  decide** — show both and their sources; if both held at different times, that is two
  dated facts, not a supersede.
- **`skip`** — always with a `reason`.

**Read before you invent.** Check `merge_candidates` (they carry bodies), then
`memory_index` or `search_materials` for the same subject under another title;
`get_memory_facts([id])` reads a body (re-call the `deferred` ids).

**A false merge is worse than a missed one** — it fuses two facts irreversibly. Before
merging two stored facts, show both to the user.

**Fix only what comes back in `rejected`** and re-emit just those; a rejected `new`
naming a stored fact is a duplicate — send a `merge` against it. Address later batches
by the ids in `created` / `touched`. `revised: N` ⇒ `get_fact_history(fact_id)`, only
when the change matters.
