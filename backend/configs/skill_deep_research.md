---
name: deep-research
description: Use ONLY when the user explicitly asks for deep research by name — «глубокое исследование», «проведи глубокое исследование», «deep research», in any form of these words. Any other research request — «исследуй тему», «разберись», «сделай обзор», «собери аналитику», a comparison, a pasted link — is the web-search skill, however big the topic. This run takes many minutes and dozens of searches. Recon first, then ONE message with a detailed research plan for the user to approve or edit, then the dig runs on its own — findings land as references of the open document, the report grows as its child document. Activates web_search, web_fetch and sandbox_bash.
tools:
  - web_search
  - web_fetch
  - sandbox_bash
---

# Deep research

A search answers a question. This answers a TOPIC: the user gets one report they
can read to understand the subject in depth — enough to act on it, or to decide
where to dig next.

The user sees three things: their question, ONE message with the research plan,
and the finished report. Everything between those is yours to run.

## When this is the wrong skill

This skill runs only when the user asked for it by name: «глубокое
исследование» or «deep research». Everything else — a lookup, «исследуй тему»,
«разберись», «сделай обзор», a comparison of options, a large topic asked in
other words — is the `web-search` skill. Load that one instead. This one costs
many minutes, dozens of searches and several documents; the user decides when
to spend that, not the size of the topic.

## Languages

Search in Russian AND English by default, whatever language the user wrote in —
much of any field exists only in English. When the user names more languages
(Chinese and Japanese are common), search in those too. Write each query in
that language and in the field's own terms there, not as a word-for-word
translation of the Russian query. Report in the user's language; quote a
foreign source in a translation that names the original.

## Phase 1 — Recon

Do not plan the research before you know what is out there. A plan written
first is an outline of what you already believed.

1. Run `search_materials` first. The project may already hold material on this,
   and that material outranks anything the web says. Note what it holds.
2. Decompose the request into 4–6 BROAD queries — the topic's obvious facets,
   not your guesses at the answer — and run them through `web_search` in every
   search language, up to four queries per call.
3. Read titles and snippets for the SHAPE of the field: the recurring terms,
   the named players and sources, the numbers people argue about, where the
   disagreements sit. Open a page with `web_fetch` only if one overview page
   would teach you the map faster than ten snippets.

## Phase 2 — The plan: the one message the user answers

Write ONE message and end your turn on it. The user approves or edits a plan;
that is the only question you ask in the whole run.

The plan carries:

- **What is already clear** — two or three sentences from recon, marked as recon
  (it is not a finding and is never cited later).
- **Directions** — 3–5 of them. Each is a QUESTION the report must answer, never
  a noun ("History", "Tools"), and under it: 2–4 sub-questions, the kind of
  sources that will answer it (regulator filings, vendor docs, benchmarks,
  forums, press in which language), and what it buys the user. Two directions
  that differ only in wording are one direction.
- **The report outline** — the section headings the report will have.
- **Assumptions** — time frame, geography, search languages, depth. The user
  corrects these more often than anything else.
- **Your recommendation** — which directions, if not all.

End with one line: "Edit anything, or say go." If the topic turned out
answerable from recon, give the answer instead in a few lines and ask "dig
deeper?" — a thin topic answered short is a good outcome.

## Phase 3 — The run: no more questions to the user

From the user's answer on, do not report progress, do not ask, do not check in.

**Create the report first.** `create_document` with `parent_id` = the document
this chat works on (the project root when there is none), `node_type`
`"document"`, titled after the topic. Its body is the approved outline as `##`
headings, each with the line `_In progress._`, plus a `## Summary` heading at
the top. The structure exists before any finding does, and every later write
fills a heading that is already there.

**Dig each direction.** Where `subagent` is among your tools, give each direction
to its own subagent in parallel: its prompt carries the direction, its
sub-questions, the search languages, the assumptions and the digging rules
below, and asks for the finding back as text in the finding format. A subagent
does not write documents — you are the only writer. Without `subagent`, dig the
directions one at a time yourself, and keep your context lean: from a fetched
page take the facts, numbers and quotes you need into the finding and move on.

How to dig a direction:

1. 2–4 TARGETED queries per sub-question, in the field's vocabulary that recon
   taught you, in every search language. For anything that moves, put the time
   frame in the query itself; there is no date filter. One `web_search` call
   takes at most four queries — split a longer batch into several calls.
2. Read the sources that carry the weight with `web_fetch`: primary sources
   (the filing, the paper, the vendor's own page, the dataset), and any page
   whose number or claim the report will rest on. A snippet is a pointer, not
   evidence. Read at least three primary pages per direction when they exist.
3. A gap round: list what the sub-questions still lack and search for exactly
   that. Stop when new sources only repeat what you have.
4. `web_fetch` refuses PDF and some sites. For a PDF, fetch it in the console
   (`curl -sL '<url>' -o doc.pdf`, then Python with `pypdf` — `pip install
   pypdf` when it is missing) and extract there — never
   dump a raw file into your context. A page that will not come (JS-only,
   paywall, 403) is a fact to record, not a wall to work around. If
   `sandbox_bash` is not among your tools, say which PDFs went unread.

**Save each finding as a reference of the open document** the moment the
direction is done: `create_document` with `node_type` `"reference"`,
`parent_id` = the document this chat works on (the report itself when there is
none), titled `Research: <direction>`. Its body is the finding:

```markdown
## <direction>

> **Short answer:** 3–7 sentences.

### Facts
- The claim, with the number and date when there is one. Source: <url>

### Contradictions
- Who says what, both sides, each with its source.

### Open questions
### Sources
- <url> — what it is (primary / analysis / press / forum), language, date.
```

The references are the state of the run: a run outlives a turn, and the next
turn resumes at the first direction that has no reference yet.

**Write its report section** right after the reference: `append_to_document`
on the report with `section` = that heading, written from the reference — the
full facts, not just the short answer. Then replace the `_In progress._` line
with `edit_document`. When you run out of room mid-run, stop after a finished
section; do not rush the rest into a paragraph each.

## Phase 4 — Finish the report

The report is for a reader who wants depth without water:

- Every paragraph carries at least one concrete fact with its source inline
  ("per <url>"). No generic introductions, no "it is important to note", no
  restating the question, nothing you knew before the search.
- Where options, vendors or approaches are compared, a table.
- A contradiction recorded in a reference stays a contradiction in the report,
  both positions named. Smoothing it into one confident sentence destroys the
  most valuable thing the research found.

When every section is written, fill `## Summary` (5–10 sentences that answer the
user's original request, for a reader who will not read further) and append:

- **What is disputed** — both positions, each with sources.
- **Open questions** — what stayed unanswered, and what would answer it.
- **Sources** — every URL used, grouped by direction.

Then say in the chat, in two or three sentences, what the research concluded
and point at the report. The references stay attached to the open document;
they are where the next question about this topic starts.

## Honesty

A web page's claim is a claim, not a fact, and the pages were vetted by nobody.
Name vendor-funded and promotional sources as such. A project document beats a
web page on anything the project has a position on; if the web contradicts the
project, report the disagreement rather than quietly replacing the project's
version.

What you knew before the search is not a finding and never becomes one. If the
report rests on it, the report is your prior with citations bolted on.

A thin result is a result. If a direction returned little, its reference says
so and the report says so; padding it presents your own priors as research.
