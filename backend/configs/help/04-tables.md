# Tables

A table in Lore is a separate object inside a document. Its data is stored with the document, and the text holds only a marker of the form `![Title](table:…)`. In place of the marker the editor shows the table itself. The marker can be moved to another place in the text, and the table moves with it.

## How to create a table

- ⌘/Ctrl + ⇧ + T inserts an empty 2 × 2 table at the cursor line.
- A CSV or TSV file can be dragged into the document or uploaded with the "Upload" button in the "References" tab. The file becomes a table of this document. The limit is 5,000 rows, 200 columns and 100,000 cells.
- A table copied from a web page, Word, Excel or Google Sheets becomes a Lore table when pasted into a document.
- Ask the agent: it can create tables, add rows and columns, and fill in cells.

## How to edit

- Every cell is a small editor with the same markup as the document: bold, links, lists.
- Tab moves to the next cell, ⇧ + Tab to the previous one. Tab in the last cell takes the cursor out of the table.
- Enter breaks a line inside a cell; it does not create a new table row.
- Rows and columns are added and removed with the buttons in the bottom-right corner of the table: insert a row below, delete a row, insert a column to the right, delete a column. Deletion takes a second press. The last row or the last column cannot be deleted.
- Column width changes by dragging its border in the header.

## Tables in the "References" tab

All of a document's tables are listed in the "References" tab next to the materials, with their size in rows × columns. Here you can:

- open a table in the center of the screen to work with a large table; the "Back to document" button returns you;
- rename a table (the pencil or a double click);
- download a table as CSV;
- delete a table together with its data (on the second press). Before deleting, Lore takes a snapshot of the document named "Before deleting table".

If you erase a table's marker from the text, the table itself does not disappear: in the list it is marked "no link in text", and the "Insert into document" button puts it back at the cursor. A table is deleted for good only with the delete button in the list.

Document snapshots also keep the state of its tables (see [Snapshots and history](help:history)).
