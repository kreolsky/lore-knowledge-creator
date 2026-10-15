# Access and publishing

## Project members

The project owner invites co-authors by email in the "Settings" tab, "Access" section. Only the owner sees this section. No email is sent: if the person already has an account with that email, the project appears in their list right away; if there is no account yet, the invitation waits with the "pending" status and takes effect on sign-up. Roles:

- **Editor** — full access: edits documents, works with the agent.
- **Commentator** — reads and leaves notes, but does not edit documents.
- **Viewer** — read-only.

The owner can open the project to all Lore users: in the "Settings" tab turn on "Public to logged-in users (read-only)". Any user with an account will be able to read the project.

## Public link

In the "Access" tab, "General access" section, you can publish a document by an anonymous link: anyone can read it, even without an account. The "Include subtree" option publishes all nested documents too. If a document is already published through its parent, you will see that.

## API keys

In the "Access" tab, "API access" section, you create keys to a document. A key has two capabilities, and both can be ticked together:

- **widget** — sends notes and files into this document: the recording apps for Android and macOS (see [Voice notes](help:voice)), or any client of the widget API.
- **agent** — MCP access for an external agent (for example, Claude or another assistant). The external agent sees only this document and its subtree. Writing is allowed by a separate checkbox; edits are applied right away and rolled back through snapshots.

The token is shown once — copy it right away.

## Connecting an external agent

An external agent needs two things: the address `<Lore address>/mcp` (the address of this site plus `/mcp`) and the key's token — "Copy token", not "Copy widget key".

An MCP-capable client takes them as a server of type http with the header `Authorization: Bearer <token>`. For Claude Code: `claude mcp add --transport http lore <Lore address>/mcp --header "Authorization: Bearer <token>"`.

Without an MCP client, send `POST <Lore address>/mcp` — stateless JSON-RPC 2.0, one request per call — with the headers `Authorization: Bearer <token>`, `Content-Type: application/json` and `Accept: application/json`, and the body `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"init","arguments":{}}}`. The result is a JSON string in `result.content[0].text`.

Tell the agent to call `init` once, first: it returns the agent's rights, its scope and the rules of work. The tools and their parameters come from the `tools/list` method.

## Arrival notifications

In the "Access" tab, "Arrival notifications" section, per document and per person: "Flag external notes" (on by default) and "Flag external references" (off by default). When on, a note or reference that arrives in this document through a key you own is marked unread for you (see [Interface](help:interface)). Arrivals while a toggle is off are never marked later.

## Moving to another project

In the "Access" tab, "Move to another project" section, a document with its whole subtree moves to another project where you have write access. You can choose a new parent in the destination project.

## Project settings

In the "Settings" tab:

- **Voice Recording** — where to save new recordings: into the current document or a specific one.
- **Image previews** — whether to show images in the references panel as a gallery of previews.
- **Public to logged-in users** — all Lore users can read the project (owner only).
- **Access** — members and invitations (owner only).
