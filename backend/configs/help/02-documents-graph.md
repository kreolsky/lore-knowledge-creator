# Documents and the graph

## Hierarchy

Documents form a tree; top-level documents sit at the project root. The tree sets the structure (a mechanic inside a system, a system inside a section) and decides which references a document sees: its own and those of all its parents (see [References](help:references)).

"Project Context", above the tree, is a special document: its references are visible from any document.

## Moving

A document always moves with its whole subtree. Two ways:

- **Drag and drop in the tree.** Drop between rows to place the document there, at any depth: at the end of a subtree, the horizontal pointer position picks the level. Drop on the middle of a row to nest the document as that row's first child, even into a collapsed document or one without children. A document cannot be dropped into its own subtree.
- **"Change parent"** button on the row: pick the new parent from a list.

Moving to another project: see [Access and publishing](help:access).

## Deleting

A document is deleted with the trash button in the tree. To confirm, you type the document's title. If the document has nested documents or references, the "Delete the whole subtree" checkbox (on by default) deletes them along with it. If you clear the checkbox, the nested documents move up one level. Deletion cannot be undone.

## Links and the graph

Links plus the hierarchy form the project graph. A document should link to the mechanics it is used in and the decisions it relies on.

- Insert a link: select text, press ⌘/Ctrl + K or the "Link" button on the selection toolbar, and pick a document. To pick a reference, start typing with `ref:`, a note with `note:`, an external address with `http://`. With no text selected, ⌘/Ctrl + K does nothing.
- Link to a heading: in the "Table of Contents" tab each heading has a "Copy link to heading" button.
- A link starting with `!` (`![text](document)`) embeds the content of the target document right into the page (see [Images and embeds](help:embeds)).
- A click on a link opens the target; hovering shows a preview.

## The "Links" tab

Outgoing links (to documents and external addresses) and incoming ones (documents that link here). Use it to check how a document is fitted into the project and to find everything that depends on it.
