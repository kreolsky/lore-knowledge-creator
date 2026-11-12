# Access and publishing

## Project members

The project owner invites co-authors in the "Settings" tab, "Access" section. Only the owner sees this section. No email is sent: if the person already has an account with that email, the project appears in their list right away; if there is no account yet, the invitation waits with the "pending" status and takes effect on sign-up. An owner who belongs to a group (see below) invites only members of that group: instead of the email field there is a picker of the group's people with a filter, and people already in the project are not offered. Roles:

- **Editor** — full access: edits documents, works with the agent.
- **Commentator** — reads and leaves notes, but does not edit documents.
- **Viewer** — read-only.

Access changes reach open documents at once: a person whose access was revoked loses the project's documents right away, without a reload.

## Group access

A group is a moderator and the users the moderator manages; groups are set up by the administrator. In the "Settings" tab, "Group access" section, a project is opened to whole groups besides the invited members:

- **An owner in a group** sets the level for their own group: Closed, Viewer, Commentator or Editor. Grants the administrator added are listed below as read-only lines; the owner can remove any of them.
- **The administrator** edits the full list: any group, or "All users" — everyone with a Lore account, at Viewer or Commentator level only.

A person's effective access is the highest of their own member row and every group grant that covers them. When the group already grants a level, invite levels at or below it are unavailable, and a member row below it shows "via group: <level>" — the level that actually applies.

## Public link

In the "Access" tab, "General access" section, you can publish a document by an anonymous link: anyone can read it, even without an account. The "Include subtree" option publishes all nested documents too. If a document is already published through its parent, you will see that.

A published link pasted into a messenger (Telegram, WhatsApp, Slack, Discord, VK and others) unfolds into a card: the document's title, the start of its text and its first picture that the link can show (otherwise the Lore picture). An unpublished or deleted document gives a plain "Lore" card that reveals nothing about it. Messengers cache cards, so an already sent link updates only when the messenger asks again.

A published [HTML page](help:html-pages) opens bare, without side panels; the link offers neither the subtree nor comments.

"Allow comments" on the link lets anyone holding it leave notes under a name of their choice and see every note on the document with its author's name; the text stays read-only (see [Notes](help:notes)). A guest's page updates as members edit and comment. Revoking the link, deleting or moving the document closes it for guests at once, and the guest's page says the connection is lost.

## Privacy

In the "Access" tab, "Privacy" section, two checkboxes:

- **Hide this page behind my PIN (only for me)** — the page opens for you only after your PIN, and the agent acting for you cannot read or find it. Other members are not affected. See [Screen and page lock](help:pin-lock).
- **Hide this document's chats** — chats started in this document and its subtree are hidden outside it, both from the chat list and from the agent's search across your chats; inside the subtree they are visible. The tree marks such a document with a crossed-out eye. Only a member with full access sets it. A [digest](help:pipelines) still reads these chats.

## API keys

In the "Access" tab, "API access" section, you create keys to a document. A key has two capabilities, and both can be ticked together:

- **widget** — sends notes and files into this document: the recording apps for Android and macOS (see [Voice notes](help:voice)), or any client of the widget API.
- **agent** — MCP access for an external agent (for example, Claude or another assistant). The external agent sees only this document and its subtree. Writing is allowed by a separate checkbox. Connecting, what the agent can do and how to undo it: [External agent (MCP)](help:external-agent).

The token is shown once — copy it right away.

A key created on the project's index document (the first row of the tree, named after the project) covers the whole project except the Agent System and this guide. Only the project owner can create keys there. On the index document the "Access" tab shows only arrival notifications and API keys: the index cannot be published by a link or moved to another project.

## Arrival notifications

In the "Access" tab, "Arrival notifications" section, per document and per person: "Flag external notes" (on by default) and "Flag external references" (off by default). When on, a note or reference that arrives in this document through a key you own is marked unread for you (see [Interface](help:interface)). Arrivals while a toggle is off are never marked later.

## Moving to another project

In the "Access" tab, "Move to another project" section, a document with its whole subtree moves to another project where you have write access. You can choose a new parent in the destination project.

## Project settings

In the "Settings" tab:

- **Voice Recording** — where to save new recordings: into the current document or a specific one.
- **Interface** — whether to show images in the references panel as a gallery of previews and whether to list notes and your chats in the "Links" tab (both for every member), and whether to play a sound when the agent finishes or asks for confirmation (for you only).
- **Group access** — the project's groups and their levels (an owner in a group, or the administrator).
- **Access** — members and invitations (owner only).

## Account tiers

Besides the role in a project, every account has a tier that the administrator or the group's moderator sets when creating or inviting a user and in the user's settings. The tier holds in every project:

- **Base** — documents, the tree, notes, search, export.
- **Voice** — plus voice recording, audio upload and transcription.
- **AI** — plus the chat and the agent, pipelines and image generation.

What the tier does not allow is hidden in the interface; an attempt anyway is refused with a message. Content already created stays readable after a downgrade. The administrator always has every capability.
