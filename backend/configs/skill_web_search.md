---
name: web-search
description: Use when the user asks to search the web, look something up online, research a topic, or read, summarize or quote a web page — including a pasted link.
tools:
  - web_search
  - web_fetch
  - sandbox_bash
---

# Web search

`web_search` takes `queries` (one to four, run together) and returns sources: title,
url, snippet, date when known. `web_fetch` returns a page's full text.

## A URL you already have is fetched, never searched

When the link is in front of you — pasted by the user, in a document, from an earlier
result — read it with `web_fetch` and answer from what came back. `web_search` finds a
URL you lack; it cannot give you the page, and its snippet is not the page. A summary of
a link is always a fetch.

A page that will not come (JS-only, paywall, 403) is the answer to report. Never answer
from the snippet, and never search the link's topic and present that as the page.

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
