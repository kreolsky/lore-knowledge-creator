---
name: web-search
description: Use when the user asks to search the web, look something up online, research a topic, or read, summarize or quote a web page — including a pasted link.
tools:
  - web_search
  - web_fetch
  - sandbox_bash
  - sandbox_run_status
  - sandbox_run_kill
---

# Web search

`web_search` takes `queries` (one to four, run together) and returns sources: title,
url, snippet, date when known. `web_fetch` returns a page's full text.

## A URL you already have is fetched, never searched

When the link is in front of you — pasted by the user, in a document, from an earlier
result — read it with `web_fetch` and answer from what came back. `web_search` finds a
URL you lack; it cannot give you the page, and its snippet is not the page. A summary of
a link is always a fetch.

A page that does not come back from `web_fetch` (403, empty, blocked, a redirect to
another site) is fetched again, in this order:

1. `curl -sL -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36' '<url>'` in `sandbox_bash`.
2. Still empty because the page builds itself in JavaScript ⇒ open it with Playwright
   (Chromium, already installed) in `sandbox_bash` and take the rendered text.
3. A run that comes back `status: running` is collected with `sandbox_run_status` and
   stopped with `sandbox_run_kill` — never left behind.

Only after this order failed is an unread page the answer to report (no `sandbox_bash`
⇒ it failed at step 1). Never answer from the snippet, and never search the link's topic
and present that as the page.

`web_fetch` refuses PDF and some content types: `curl -sL '<url>' -o doc.pdf` in
`sandbox_bash`, then extract in Python (`pip install pypdf` if missing) — never dump a
raw file into context. No `sandbox_bash` ⇒ say plainly the file could not be read.

## Where to look — the internet is last

1. **This conversation** — the attached document or reference, anything pasted. It
   overrides both below.
2. **`search_materials`** — the project's documents, references and memory.
3. **`web_search`** — only when 1 and 2 do not answer, or the question is about the
   outside world (current events, an external source).

A textbook subject is no exception: the project may hold its own position, and it wins.
If the web contradicts the project, report the disagreement; never silently replace the
project's version.

## Searching

- Focused queries; when one wording may miss, send 2–4 phrasings in ONE call.
- No date filter: put the time frame in the query ("2026", "this week").
- "Showing the first N sources" means more exist — refine before treating the topic as
  covered.
- Every claim from outside carries its source: "per <url>".

## A research request: when you are done

"Собери / исследуй / сделай обзор / сравни / найди критику" asks for a topic, not a
fact. The answer still goes in chat; what changes is when you stop searching.

- Before the first search, put the topic's facets on the todo list (`todo_write`) —
  the approaches, the evidence, the criticism, whatever the request names. Mark a facet
  done only when its sources are read; write the answer when every item is done.
- Read before you write: the answer rests on pages you fetched in full, not on
  snippets. A topic with several facets usually needs 10+ fetched sources; a facet
  with one source is reported as thin.
- Search each facet in Russian AND English, in the field's own terms; every term the
  first results teach you is a new query.
- Criticism, limitations and failures are searched for on their own ("criticism",
  "limitations", "failed", "postmortem", "критика", "провал") — they do not surface in
  queries about the approach itself.
- Stop when a new round of queries brings back only sources you already read. "I think
  I have enough" is not that test.
- Length follows the material: every source that changed the picture appears with its
  number, date and link. Do not compress the answer to a target length.
- A topic with four or more independent directions, or one the user will act on, is
  deep research: say so in one sentence at the end and offer it.
