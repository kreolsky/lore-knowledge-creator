---
name: memory-consolidation
description: Use when the user wants to consolidate, merge, clean up, or verify project memory from a document's references — distil the attached material into memory facts (one fact per document). Сконсолидировать / объединить / почистить / сверить память проекта; работает по ссылкам документа. Loads the consolidation procedure and its tool pack.
tools:
  - consolidate_memory
  - get_memory_facts
  - apply_memory_verdicts
  - next_reference
  - get_fact_history
  - reopen_consolidation
---

# Memory consolidation

Distil a document's reference material into project memory: each **fact** is one
document whose body IS the fact. The run is a closed loop — `consolidate_memory` →
read → `apply_memory_verdicts` → `next_reference` → repeat — until it says `complete`.

## The loop

1. **Resolve the target.** The user names a document (or an open reference — the run
   re-anchors onto its host and covers its whole stack). If they named neither, the
   target is the document this chat is open on — the one the `# Current document`
   section names — so take it, do not ask. The default breadth is the target's whole
   subtree: call `get_project_structure` with the target as `start_id` and consolidate
   every document it lists (the target itself plus its descendants), one full run per
   document, each closed to `complete` before the next begins; a request to consolidate
   "this document only" narrows the breadth to a single run. Call
   `consolidate_memory(target_doc_id)`. It returns this portion's
   `reference` (the ONE piece of material to consolidate now — a **window** of one
   reference; long material is sliced into ~12 000-char windows that overlap at the
   seams, and `reference.window = {index, total, from, to}` names which window this is),
   `memory_index` (every stored fact as id + title), `merge_candidates` (the facts nearest
   this material, each **with its body**), `progress` (references AND windows), and
   `stats_at_start` (the project's opening shape — measure the run's delta against **it**,
   not a fresh census). It carries `run_id`; you do not pass it back — the loop tools
   resolve the active run from your session. Say which document you are consolidating
   before going on.
2. **Consolidate THIS window** fully, apply your verdicts, then call
   `next_reference(completed_reference_id, completed_window_index)` to stamp it and
   receive the next portion. `completed_window_index` is the `index` from the portion's
   `reference.window` — pass it so a retried call after a lost response cannot skip a
   window. Repeat until the result says `complete: true`. One window per portion is the
   point — a stamped window's material leaves your context, so finish each before you
   stamp it. A reference longer than one window is served across several portions; the
   run moves to the next reference only when the last window of the current one is
   stamped.
   - `reference.window` advancing (same reference, next index) is normal continuation,
     not a new reference — keep going.
   - A fact that straddles a seam appears in BOTH windows (they overlap) and will meet
     its twin as a `merge` candidate on the second pass — merge it, do not write it again.
   - `reference: null` + `waiting: true` → the next reference is still transcribing.
     Stop and say so; consolidate again later.
   - `reference: null` + `complete: true` → the run is done; report it.

## Writing a fact

A fact is ONE assertion. Its `title` reads as a name — `Субъект: краткая суть`
(`Аспирин: антиагрегант при риске нарушения функции плаценты`) — and its `text` body IS
the fact: plain prose, stored verbatim, and nothing else — a link in a body is text
the next run has to match around. This is the rule that matters most: a body wrongly
promoted to a title (`Роль тромбоцитов при имплантации`) is unmatchable, so every
later run deposits another disposable layer instead of accumulating.

Write in the project's language and in the wording the material uses. A fact states what
is true, and it must read that way to someone who has neither the material nor this
session. Keep every qualifier that bounds WHAT is claimed — the condition it holds
under, the period, the population, the dose — because a claim stripped of its condition
is a false universal. Drop everything that only says WHERE you read it: **a fact never
refers to the material, the speaker or this session.** The server stamps every fact with
this portion's `reference_id`, so its source is already recorded and repeating it in the
prose costs the opening of every body. A dated fact is valuable — keep it, putting the
date in the wording.

A reference is finished when there is nothing left worth saying about it — not when you
have emitted one batch. Read it, extract every fact it carries, apply, then read it
AGAIN for what the first pass dropped (asides, dates, the second drug in a sentence
about the first, a relation stated in passing); keep going until a re-read turns up
nothing new. The first pass always drops things — that is why re-reading is the job,
not an extra.

## Verdicts — one per fact; the unit of work is the reference

Emit one verdict per fact. Apply via `apply_memory_verdicts(reference_id, verdicts)` in
batches that FIT — split a batch only when it is too large to emit in one call (a batch
that does not fit the model's output limit truncates mid-JSON and never succeeds,
however many times it is retried). Use as many batches as a reference needs. Cite this
portion's `reference_id` on every batch.

- **`new`** — knowledge not yet in memory. Give `title` (subject-led) + `text`.
- **`merge`** — the same knowledge as a stored fact, arriving again. Give `fact_id`
  (+ `text` to update the wording; add `absorb_id` to fold a SECOND stored fact in).
  The server picks the survivor; the absorbed fact retires pointing at it. A merge that
  adds no new source is dedup, not confirmation.
- **`supersede`** — this CORRECTS a stored fact in place: the old wording is kept as
  history and the doc id stays, so links survive. Give `fact_id` + `reason` + `text`.
  **The target is the `fact_id` FIELD — an id written inside fact text is prose and
  connects nothing.** On a contradiction, **ask — do not decide**: show the stored fact,
  the new one and where each came from; or if both held at different times, that is two
  facts each stating its own period, not a supersede.
- **`skip`** — not worth remembering, or not resolvable now. Always give a `reason`.

**Read before you invent.** `merge_candidates` is the server's shortlist of the facts
nearest this material, each arriving **with its body** — so a fact you would write as
`new` can be checked against what is stored without any extra call. Beyond it, scan
`memory_index` for the same subject under another title; when one looks promising, call
`get_memory_facts([id])` to read its body before you merge or reuse it (a `deferred`
list means re-call with just those ids).

**Merge is more dangerous than supersede.** A missed merge is fixable next run; a false
merge fuses two facts irreversibly — the absorbed wording no longer shows there were
ever two, and nothing later reveals it. Before merging two facts, show both sides and
let the user look.

**The `rejected` list.** The server applies the valid verdicts and returns the invalid
ones in `rejected`, each with its correction. Fix ONLY those and re-emit just them —
re-emitting the whole batch creates duplicates. A `new` in `rejected` whose reason names
a stored fact is the duplicate gate: the fact restates that stored one — send a `merge`
against it instead.

**Carry ids forward.** The result names every fact it wrote — `created` (new fact-doc
ids) and `touched` (existing ones a merge/supersede changed). Take ids from there to
address the same facts in later batches. A served fact marked `revised: N` was
superseded `N` times; call `get_fact_history(fact_id)` to read what it replaced,
**only when the change matters**.

## Re-opening a target

A finished target is stamped done, so a second `consolidate_memory` over it is inert.
When the user asks to **redo** a target they have already consolidated — to steer a
second pass with a new focus, or under a different model — call
`reopen_consolidation(target_doc_id)`. It clears the consumed-stamps on the target's
references (a reference id re-resolves to its host, as with `consolidate_memory`) and
stops: **no fact is touched, and nothing is recorded about why.** The next
`consolidate_memory` then re-serves every reference under that host, oldest-first.

The two things that make a repeat non-identical need no tool support — carry the focus
in your chat text, and the model is chosen per session. Re-extraction is not dirty: the
existing facts appear in `memory_index` and `merge_candidates` **with their bodies**, so
overlapping material lands as `merge` / `supersede` rather than a forked twin. Reach for
this ONLY on an explicit re-consolidation request — never as a step of the normal loop.

## Close with a report

Nothing remembers an open question for you. End with: the run's **delta** (created /
touched, read off the tool results and measured against `stats_at_start` — not a fresh
census; a `nothing_to_do` run is zero); the memory shape (`stats.facts` live,
`stats.retired` retired); the per-reference shape (`reference_shape` on the closing
portion — facts and apply-batches per reference; say it plainly, it is what the run is
judged on, and a reference that came out at a uniform small count is the quota this run
is meant to avoid); where the run ended (`progress`); references you could not read
(`unusable_skipped` / `waiting`); what you skipped and why; and any question you asked
and did not get an answer to, verbatim.
