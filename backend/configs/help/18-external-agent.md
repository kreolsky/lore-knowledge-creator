# External agent (MCP)

Any MCP-capable agent — Claude Code, Claude Desktop, Cursor, your own script — can work with a Lore project from outside: read, search, write documents and tables, upload files. It connects to the project's MCP server with an API key.

## The key

1. Open the document the agent should work in, the "Access" tab, "API access" section.
2. Create a key with "agent" ticked. Tick the write checkbox if the agent may change anything; without it the key is read-only.
3. Press "Copy token" (not "Copy widget key") — the token is shown once.

What the key decides:

- **Scope** — the agent sees only this document and its subtree. Anything outside answers "access denied". To give it the whole project, create the key on the project's index document. A system document (Agent System) cannot be a key's root.
- **Identity** — the agent acts as you, the key's owner, and never exceeds your rights. If you lose access to the project, the key stops working.
- **Writing** — read-only or read-write, set at creation.
- **Who can create one** — a member with full (Editor) access to the project.
- **Revoking** — delete the key in the same section; it stops working at once. Rename it to tell keys apart.

## Connecting

The agent needs the address `<Lore address>/mcp` (the address of this site plus `/mcp`) and the token, sent as the header `Authorization: Bearer <token>`. The server is of type http (streamable HTTP).

Claude Code:

```
claude mcp add --transport http lore <Lore address>/mcp --header "Authorization: Bearer <token>"
```

The same server in a project's `.mcp.json`:

```json
{"mcpServers": {"lore": {"type": "http", "url": "<Lore address>/mcp", "headers": {"Authorization": "Bearer <token>"}}}}
```

Other clients take the same two things — the URL and the header — in their own settings.

Without an MCP client, send `POST <Lore address>/mcp` — stateless JSON-RPC 2.0, one request per call — with the headers `Authorization: Bearer <token>`, `Content-Type: application/json` and `Accept: application/json`, and the body `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"init","arguments":{}}}`. The result is a JSON string in `result.content[0].text`. `"method":"tools/list"` lists the tools with their parameters.

## The first call: init

Tell the agent to call `init` once, first. It returns the agent's rights (read-only or read-write), its scope and the rules of work, plus your project's Agent System: the rules in full and the lists of skills and knowledge, which the agent reads on demand. So the rules you wrote for the built-in agent (see [Personas, rules and knowledge](help:personas-rules-knowledge)) apply to the external one too. Personas do not.

## What the agent can do

Reading:

- semantic search over documents, references and project memory (`search_materials`);
- reading a document with its references, tables and processing status (`read_document`), and the tree (`get_project_structure`);
- downloading a node's original file by a short-lived link (`get_file`);
- exporting a whole subtree as a zip of Markdown files for GitHub or Obsidian (`export_subtree_md`).

Writing (read-write key only):

- creating documents and text references (`create_document`);
- pointwise edits and appending to the end or to a section (`edit_document`, `append_to_document`);
- tables: create, edit cells, add rows and columns (`create_table`, `edit_table_cell`, `add_table_rows`, `add_table_column`);
- moving and renaming, including turning a document into a reference and back (`move_document`, `rename_document`);
- uploading files — audio, images, .docx, Markdown (`attach_file`): the agent gets a short-lived link and sends the bytes to it itself; the text (transcript, markdown) appears asynchronously, as with an upload in the interface. A failed conversion is re-run with `reprocess_file`;
- importing a zip of Markdown files (an Obsidian vault, an export) as a subtree (`import_subtree_md`): folders become documents, files in `_assets/` become references, tables become editable tables, links are re-linked.

Uploading audio requires an account tier with voice (see [Access and publishing](help:access)).

## What it cannot do

- Delete documents — that is the user's job.
- Chat history, memory curation, saving skills, the sandbox console, image generation and web search — these belong to the built-in agent only.
- Wait for your approval: there is no confirmation step over MCP. On a read-write key every write applies at once.

## Undoing its work

- Content edits (text, tables) record a snapshot before the edit: undo them in [Snapshots and history](help:history).
- Moves and renames record nothing: undo them by moving or renaming back.
- Imported documents and uploaded files are removed like any others; the deletion itself can be undone.

New notes and references the agent adds can be marked unread for you (see "Arrival notifications" in [Access and publishing](help:access)).

## Limits and errors

- 30 writes per minute per key; 240 requests per minute per key; 600 requests per minute from one address. Over the limit the answer is 429 — wait a minute.
- An error comes back as `{"error", "status_code", "next_action"}`: 401 — the key is invalid or deleted; 403 — no rights, outside the scope, or a write on a read-only key; 404 — no such document; 409 — the text changed since it was read, re-read it; 422 — an edit tried to replace almost the whole document, split it into small edits.
