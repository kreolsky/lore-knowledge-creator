---
name: deep-research
description: Use ONLY when the user asks for deep research by name — «глубокое исследование», «deep research», in any form. Any other research request («исследуй тему», «разберись», «сделай обзор», a comparison, a pasted link) is web-search, however big the topic. A long run — plan first, approved by the user.
tools:
  - web_search
  - web_fetch
  - sandbox_bash
  - sandbox_run_status
  - sandbox_run_kill
---

# Deep research

A search answers a question; this answers a TOPIC — one report deep enough to act on or
to decide where to dig next. The user sees three things: their request, ONE plan
message, the finished report. Everything between is yours to run.

Only the user's explicit «глубокое исследование» / "deep research" starts this; any
other research request is the `web-search` skill — load that one instead.

**Web rules.**
- A URL you already have is read with `web_fetch`, never searched for.
- `web_fetch` refuses PDF: `curl -sL '<url>' -o doc.pdf` in `sandbox_bash`, extract
  with Python `pypdf` (`pip install pypdf` if missing) — never dump a raw file into
  context. No `sandbox_bash` ⇒ say which PDFs went unread.
- A page `web_fetch` does not return (403, empty, blocked, a redirect to another site)
  is fetched again in this order: `curl -sL -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36' '<url>'` in
  `sandbox_bash`; a JavaScript-only page ⇒ Playwright (Chromium, installed) in
  `sandbox_bash`; a run that comes back `status: running` is collected with
  `sandbox_run_status` or stopped with `sandbox_run_kill`. Only after this order failed
  is the page recorded as unread.
- A project document beats a web page on anything the project has a position on; when
  they disagree, report the disagreement instead of replacing the project's version.

**Languages.** Search in Russian AND English by default, plus any language the user
names; write each query in that language's own field terms, not as a translation.
Report in the user's language; a quoted foreign source names the original.

## Phase 1 — Recon

Know the field before planning it — a plan written first outlines what you already
believed.

1. `search_materials` — what the project already holds outranks the web.
2. 4–6 BROAD queries over the topic's obvious facets, in every search language, via
   `web_search`.
3. From titles and snippets take the shape: recurring terms, named players and sources,
   disputed numbers, where disagreements sit. `web_fetch` one overview page only if it
   maps the field faster than snippets.

## Phase 2 — The plan: the only question you ask

ONE message, then end the turn:

- **Already clear** — 2–3 sentences, marked as recon (never cited later).
- **Directions** — 3–5, each a QUESTION the report must answer (never a noun like
  "History"), with 2–4 sub-questions, the kind of sources that answer it, and what it
  buys the user. Two directions that differ only in wording are one.
- **Report outline** — two levels: a `##` section per direction and a `###`
  subsection per sub-question, each with one line saying what it will contain
  (a number, a comparison, a list). This outline is the contract the run fills.
- **Assumptions** — time frame, geography, search languages, depth.
- **Recommendation** — which directions, if not all.

End with "Edit anything, or say go." If recon already answers the topic, answer in a few
lines and ask "dig deeper?" instead.

## Phase 3 — The run: no more questions, no progress reports

**Create the report first**: `create_document`, `parent_id` = the document this chat
works on (project root if none), `node_type` `"document"`, titled after the topic; body
= `## Summary` at the top plus the approved two-level outline, each heading with
`_In progress._`.

**Dig each direction.** With `subagent` among your tools, give each direction to its
own subagent in parallel — prompt carries the direction, sub-questions, languages,
assumptions, the web rules above and the digging rules below, and asks for the finding
back as text in the finding format, every fact with its URL — the report is written
from that text alone; you stay the only writer (a subagent has no document tools). The
prompt also tells the subagent to put its sub-questions on its own todo list
(`todo_write`) and close each only when it is answered from fetched sources or recorded
as thin, and to keep every sandbox file in its own directory — first command
`mkdir -p $HOME/research/<random> && cd $HOME/research/<random>`, every later path
inside it — because parallel subagents share one workspace. Without it, dig one at a time and keep only
the facts, numbers and quotes from each page.

1. 2–4 TARGETED queries per sub-question in the vocabulary recon taught you, every
   search language; time frame in the query (no date filter); at most four queries per
   `web_search` call.
2. `web_fetch` the sources the report will rest on — primary ones (filing, paper,
   vendor page, dataset); at least three per direction when they exist. A snippet is a
   pointer, not evidence.
3. Gap round: search for exactly what the sub-questions still lack; stop when new
   sources only repeat.

**Save the finding** when a direction is done: `create_document`, `node_type`
`"reference"`, `parent_id` = the report's id, titled `Research: <direction>`. Hosted on
the report, the finding is visible from it and travels with it.

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

These references are the run's state: a later turn resumes at the first direction
without one.

**Then its report section**: `append_to_document` with `section` = that heading, full
facts from the reference, then `edit_document` away its `_In progress._` line. Out of
room mid-run ⇒ stop after a finished section; never rush the rest.

## Phase 4 — Finish

- Every paragraph carries a concrete fact with its source inline ("per <url>") — no
  introductions, no restating the question, nothing you knew before searching.
- Comparisons of options or vendors go in a table.
- A recorded contradiction stays one in the report, both positions named.

Fill `## Summary` (5–10 sentences answering the original request) and append **What is
disputed**, **Open questions** (and what would answer them), **Sources** (every URL, by
direction). In the chat, say in 2–3 sentences what the research concluded and point at
the report.

## Honesty

A web page's claim is a claim; name vendor-funded and promotional sources as such. Your
prior knowledge is never a finding. A thin direction is reported as thin — padding
passes your priors off as research.
