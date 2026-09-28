---
name: deep-research
description: Use when the user wants a topic INVESTIGATED, not looked up — «проведи глубокий поиск», «исследуй тему», «собери аналитику», «разберись в теме», «сделай обзор», deep research, research this properly, comprehensive overview. Runs recon, then a research plan, then a dig per section, and lands one compiled document with sources. Activates web_search and sandbox_bash.
tools:
  - web_search
  - sandbox_bash
---

# Deep research

A search answers a question. This answers a TOPIC: it comes back as a document
the user can read to understand the subject well enough to decide where to dig
next.

## When this is the wrong skill

One question with one answer — a date, a version number, what a page says — is
the `web-search` skill. Load that one instead. This one costs many searches and
a document; spending it on a lookup is waste, and the user asked for a lookup.

The signal for THIS skill is that the answer has parts: several sub-questions,
competing positions, a landscape to map.

## Phase 1 — Recon

Do not plan the research before you know what is out there. Planning first
produces an outline of what you already believed.

1. Decompose the request into 3–5 BROAD queries — the topic's obvious facets,
   not your guesses at the answer.
2. Run `search_materials` first. The project may already hold material on this,
   and that material outranks anything the web says. Say what it holds.
3. Run those queries through `web_search` — up to four per call in `queries`.
   You are reading titles and snippets for the SHAPE of the field: what the recurring terms are, who
   the named sources are, where the disagreements sit.

Recon is cheap and shallow on purpose. Do not open pages yet.

## Phase 2 — The research plan

From the recon, write an outline: 4–8 sections, each one a QUESTION the final
document must answer, with a line on what would answer it. An outline of nouns
("History", "Tools") is not a plan; an outline of questions is.

Print the outline in the chat so the user sees where the research is going, then
KEEP WORKING in the same turn. Do not ask for approval — the user can redirect
you after seeing it, and stopping to ask costs them a turn for nothing.

Then create the report document immediately: `create_document`, titled after the
topic, `parent_id` = the document this chat is working on (the project root when
there is none), body = the outline as headings plus a one-line "in progress"
note. This document is now where the research lives.

## Phase 3 — The digs

Take the sections in order. For each one:

- 2–4 TARGETED queries in one `web_search` call — the section's question in the
  field's own vocabulary, which recon just taught you. For anything that moves,
  put the time frame in the query itself; there is no date filter.
- A "Showing the first N sources" note means the topic is not exhausted: refine
  before you conclude.
- When a snippet is not enough — the claim matters, the source is primary, the
  numbers are in the page — fetch it in the console:

      curl -sL '<url>' -o page.html

  and strip it there with Python. Never dump raw HTML into your context. A page
  that will not come (JS-only, paywall, 403) is a fact to record, not a wall to
  work around; if `sandbox_bash` is not among your tools this deploy serves no
  console — work from snippets and say so.

- Write the section into the document with `append_to_document` BEFORE starting
  the next one, each claim carrying its source ("per <url>").

Appending as you go is not tidiness. A turn has a tool-call ceiling and long
research hits it: the document holds what is finished, so the next turn resumes
at the first unwritten section instead of starting over. When you run out of
room mid-research, say which sections are done and stop — do not rush the
remaining ones into a paragraph each.

## Phase 4 — Compile

With the sections written, go back to the top of the document (`edit_document`)
and add:

- **A summary** — 5–10 sentences that answer the user's original request, for a
  reader who will not read further.
- **What is disputed** — where sources contradicted each other, both positions
  named. Smoothing a disagreement into one confident sentence destroys the most
  valuable thing the research found.
- **Open questions** — what stayed unanswered, and what would answer it. This is
  where the user decides whether to dig further.
- **Sources** — the URLs, so a claim can be checked.

Then say in the chat, in two or three sentences, what the research concluded and
point at the document.

## Honesty

The web's claim is a claim, not a fact, and the pages were vetted by nobody. A
project document beats a web page on anything the project has a position on; if
the web contradicts the project, report the disagreement rather than quietly
replacing the project's version.

A thin result is a result. If a topic returned little, the document says so and
stays short — padding it with what you already knew presents your own priors as
research, which is exactly what the user asked you to avoid.
