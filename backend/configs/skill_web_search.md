---
name: web-search
description: Use when the user asks to search the web, look something up online, research a topic, or read, summarize or quote a web page — including one whose link they pasted. Search returns snippets; a page's full text comes from web_fetch (PDF through the sandbox console). Activates web_search, web_fetch and sandbox_bash.
tools:
  - web_search
  - web_fetch
  - sandbox_bash
---

# Web search

`web_search` takes `queries` — one to four search strings, run together — and
returns one list of sources: title, url, a snippet, and the publication date when
the provider has one. A note "Showing the first N sources" means the list was cut.

## You already have the URL — fetch it, do not search

When the link is already in front of you — the user pasted it, a document
carries it, an earlier result returned it — searching for it is the wrong
move: the search engine cannot give you the page, and its snippet is not what
the user asked you to read. Read it with `web_fetch`, which returns the page
as text, then answer from what came back. `web_search` is for finding a URL
you do not have; `web_fetch` is for reading one you do. A summary of a link is
always the second of those.

## Full text of a page

`web_search` returns snippets. When the task needs the whole page — an
article, documentation, a thread — call `web_fetch` on its URL. The citation
habit does not change: the claim carries its source ("per <url>").

`web_fetch` does not read PDF and refuses some content types. For those, this
skill also activates `sandbox_bash`: fetch the file in the console

    curl -sL '<url>' -o doc.pdf

and extract the text there in Python (`pip install pypdf` when no PDF
library is installed) — never dump a raw file into your context. If
`sandbox_bash` is not among your tools, this deploy serves no console: say
plainly that the file could not be read.

A page that will not come — JS-only rendering, a paywall, a 403 — is an
answer to report, not to work around; say so instead of answering from the
snippet, and never search for the link's topic and present the result as a
summary of the page.

## Order of sources — the internet is the LAST one

This orders where to LOOK for an answer. It does not apply once the source is
already named: a link the user handed you is the material they are asking
about, and it is fetched, not searched for.

1. **What is already in this conversation** — the attached document or reference, and
   anything the user pasted. It is the most authoritative source there is: it is what
   they are working on, and it overrides both of the below.
2. **`search_materials`** — this project's own documents, references and memory.
3. **`web_search`** — only when 1 and 2 do not answer, or the question is explicitly
   about the outside world (current events, an external source, a link).

A well-known or textbook subject is NOT an exception. The project may hold its own
established position on it, and that position wins over anything the internet says —
so search the project first even when you believe you already know the answer. If the
web contradicts the project's material, report the disagreement; do not silently
replace the project's version with the internet's.

- Use a focused query. When one wording may miss, send two to four phrasings in
  the same call rather than one call per phrasing.
- There is no date filter: for current events, put the time frame in the query
  itself ("2026", "this week").
- A "Showing the first N sources" note means more results exist: refine the
  query before you treat the topic as covered.
- Carry the source URL into your answer with the claim it supports ("per <url>"), so
  the user can see which sentence came from outside and check it.
