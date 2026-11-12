# HTML pages

An HTML page is a document that holds a web page instead of Markdown text: a dashboard, a calculator, an interactive chart, a small tool. It sits in the tree like any document, with its own icon.

## How to get one

- Ask the agent: "make an interactive page that…". It builds the page and saves it under the document you name.
- To turn an existing `.html` file into a page, attach the file in the chat and ask the agent to import it as a page.

A page is limited to 5 MB by default; the administrator can change the limit, and a refusal names it. If the server has no page hosting configured, the page says so instead of showing its content.

Asked to rework an existing page, the agent rewrites it in place: the page keeps its place in the tree, its links, notes and snapshots, and a snapshot is taken before the rewrite. With "Auto" unchecked, the rewrite waits for your confirmation like any other edit.

## Viewing and editing

The page runs in an isolated frame from a separate address, so its scripts cannot reach your Lore session. If the page tries to open another address, Lore stops it and says so.

The page's toolbar:

- **Page / Source** — switches between the running page and its source. The source can be edited by those who can edit the document; an edit that would push the page over the limit is refused.
- **Publish** — publishes the page by a public link (copy the link, or revoke it). See [Access and publishing](help:access).
- **Download** — the page as an `.html` file, as is.
- **Fullscreen** — expands the page with its toolbar to the whole screen; the same button or Escape returns.

A page has no child documents, but it can hold references. Notes are left on the whole page (see [Notes](help:notes)). Embedding a page into another document shows a link to it, not the page itself.

## Published pages

A published page opens without side panels, as a bare page. If the page declares a light or dark color scheme, it is shown in that scheme; your own theme returns when you leave it.
