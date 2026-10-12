# Documents and the graph

## Hierarchy

Documents nest inside each other and form a tree; top-level documents sit at the project root. The tree sets the structure: a mechanic lives inside a system, a system inside a section. A document's place in the tree decides which references it sees: its own references and those of all its parents (see [References](help:references)).

A document is moved with the "Change parent" button on the document in the tree. Its whole subtree moves with it. Dragging in the tree only changes the order of documents on one level. Moving to another project is described in [Access and publishing](help:access).

Above the tree there is a special document, "Project Context". References attached to it are visible from any document.

## Deleting

A document is deleted with the trash button in the tree. To confirm, you type the document's title. If the document has nested documents or references, the "Delete the whole subtree" checkbox (on by default) deletes them along with it. If you clear the checkbox, the nested documents move up one level. Deletion cannot be undone.

## Links and the graph

Any document can link to any other. Links together with the hierarchy form the project graph. Every document should link to the mechanics it is used in and to the decisions it relies on.

- Insert a link: select text, press ⌘/Ctrl + K or the "Link" button on the selection toolbar, and pick a document. To pick a reference, start typing with `ref:`, a note with `note:`, an external address with `http://`. With no text selected, ⌘/Ctrl + K does nothing.
- Link to a heading: in the "Table of Contents" tab each heading has a "Copy link to heading" button.
- A link starting with `!` (`![text](document)`) embeds the content of the target document right into the page (see [Images and embeds](help:embeds)).
- A click on a link opens the target; hovering shows a preview.

## The "Links" tab

The "Links" tab of the right panel shows the document's outgoing links (to documents and external addresses) and incoming ones — the documents that link to this one. It is a handy way to check that a document is fitted into the project and to find everything that depends on it.
