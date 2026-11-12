# Interface

Three areas: the left panel, the center, the right panel.

## Left panel

- **Documents** — the project tree. Row buttons: add a child document, rename, change parent, delete. Drag and drop moves a document (see [Documents and the graph](help:documents-graph)).
- **Table of Contents** — headings of the open document; a click jumps to the heading.
- ⌘/Ctrl + \ hides and shows the left panel.
- The pin under the tabs switches the panel between docked (the default) and auto-hide. Unpinned, the panel slides over the editor when the pointer reaches the tab strip and hides a moment after the pointer leaves; its tabs switch by click.

## Center

The open document, reference or [HTML page](help:html-pages). Split view shows the document on the left and a reference on the right (see [References](help:references)).

The text column has three widths: normal, wide and wider. Ctrl + Alt + ] widens it and Ctrl + Alt + [ narrows it (on macOS Control + Option, not ⌘). The width is remembered per project in this browser; on a narrow window the column never exceeds the space available.

## Right panel

The available tabs depend on the role: a viewer, for example, has no Chat and no Notes. Chat also needs an account tier with AI (see [Access and publishing](help:access)). A page locked with your PIN offers only Chat, Search and Settings until the PIN is entered (see [Screen and page lock](help:pin-lock)).

- **Chat** — the agent (see [Chat and the agent](help:chat-agent)).
- **References** — the document's materials, tables and pipelines (see [References](help:references)).
- **Notes** — discussions attached to text (see [Notes](help:notes)).
- **Links** — outgoing and incoming links (see [Documents and the graph](help:documents-graph)).
- **Snapshots** — snapshots and history (see [Snapshots and history](help:history)).
- **Search** — documents, memory, your chats (see [Search](help:search)). The temporary **Find & Replace** tab appears on ⌘/Ctrl + F.
- **Access** — public links, privacy (page lock, hiding chats), API keys, arrival notifications, moving to another project (see [Access and publishing](help:access)). On the project's index document it holds only arrival notifications and API keys.
- **Settings** — project settings: where voice recordings land, the "Interface" section (image previews, notes and chats in the "Links" tab, the agent's sound), members and group access.

## Header

Voice recording for the open document (accounts with the voice tier or higher), theme and language switches, [profile settings](help:profile), sign-out. The "Lock screen" button appears once a PIN is set (see [Screen and page lock](help:pin-lock)).

## After an update

When a new Lore version is installed, a tab opened before it shows "Lore was updated" with a "Reload" button and stops syncing edits until it is reloaded, so an old tab never writes in an outdated format.

## Unread arrivals

Notes and references that arrive from outside — from the recording apps (widget key) or an external agent (MCP key) — are marked unread for the owner of that key. The mark shows on the item, on the Notes / References tab, and on the document's title in the tree; a collapsed parent gets a muted mark. Opening the item clears it. Other members never see your marks. What gets marked is set per document (see [Access and publishing](help:access)).

## Narrow screens and touch

On a narrow screen (a phone) only the center is shown; the "Documents" button opens the left panel and "Open panel" the right panel as drawers over it. On touch devices hover previews are off, row menus open on tap, and actions that normally appear on hover are always visible. On a narrow screen a swipe over the document tree or the references list scrolls it; reordering by drag works on a wide screen only.
