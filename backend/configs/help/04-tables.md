# Tables

A table in Lore is a separate object inside a document. Its data is stored with the document, and the text holds only a marker of the form `![Title](table:…)`. In place of the marker the editor shows the table itself. The marker can be moved to another place in the text, and the table moves with it.

A table stands on its own line, like a picture: the cursor, clicks and arrow keys treat it as one block. A click on the empty part of the table puts the cursor next to it — before the table near its top, after it otherwise. A table selected as a whole shows the same frame as a selected picture.

## How to create a table

- ⌘/Ctrl + ⇧ + T inserts an empty 2 × 2 table at the cursor line.
- A CSV or TSV file can be dragged into the document or uploaded with the "Upload" button in the "References" tab. The file becomes a table of this document. The limit is 5,000 rows, 200 columns and 100,000 cells.
- A table copied from a web page, Word, Excel or Google Sheets becomes a Lore table when pasted into a document.
- Ask the agent: it can create tables, add rows and columns, and fill in cells.

A new table gets column widths that follow its content: a long column is wider, a short one narrower. An empty table inserted with ⌘/Ctrl + ⇧ + T takes part of the text width, leaving room for more columns. A table with merged cells pasted from Google Docs, Sheets, Word or Excel keeps its merges.

## How to edit

- Every cell is a small editor with the same markup as the document: bold, links, lists.
- Tab moves to the next cell, ⇧ + Tab to the previous one. Tab in the last cell adds a new row and moves to its first cell.
- Enter breaks a line inside a cell; it does not create a new table row.
- The cell with the cursor is outlined with a frame.
- **Selecting cells.** One click edits a cell. Esc in a cell, ⇧ + click on another cell, or a drag across cells selects a rectangle of cells. Arrows move the selection, ⇧ + arrows grow it, Tab walks the cells without adding rows. Typing, Enter or F2 edits the highlighted head cell, ⌘/Ctrl + V replaces its text, Delete clears every selected cell in one undo step, Esc puts the cursor after the table.
- **Merging cells.** Select a range and choose "Merge cells" in its right-click menu; when other cells of the range hold text, the menu item says how many will lose it. "Unmerge cells" keeps the text in the top-left cell. Arrows, Tab, selection, Find and Backspace row deletion treat a merged area as one cell; a row or column cannot be moved across a merged area.
- **Right-click on a cell** opens its menu: insert a row above or below, insert a column to the left or right, move the row up or down, move the column left or right, delete the row, delete the column, delete the table. ⇧ + right-click opens the browser's own menu instead.
- Backspace at the start of an empty row's first cell deletes that row.
- Every column has a fixed width; no column stretches on its own. A column inserted from the menu takes the width of the column you clicked.
- Dragging a column's border changes the width of that column only; a double click on the border fits the column to its longest line. The corner handle on the table's frame scales the whole table, keeping the proportions of its columns; a double click on it keeps the table's width and lays out the columns by their content: short columns stay on one line, long ones share the rest. While you hover or drag a handle, the edges it moves are highlighted. A table wider than the page scrolls inside its own block.
- Undo (⌘/Ctrl + Z) steps back through one history for the whole document: text, cell edits, row and column operations and resizes undo in the order they happened. Another person's or the agent's edits are never undone by your ⌘/Ctrl + Z.
- Pictures in cells work as in the text: drop or paste them into a cell (see [Images and embeds](help:embeds)). They share one gallery with the pictures in the text.
- Links, external links included, and [notes](help:notes) work inside cells: a note made in a cell is attached to that cell's text. Links in cells show up in the "Links" tab.
- Find and replace (⌘/Ctrl + F) also searches the cells: matches are counted, highlighted and stepped through, and "Replace all" changes cells too (see [Search](help:search)).
- A viewer or a commentator cannot edit cells or change widths.

## Tables in the "References" tab

All of a document's tables are listed in the "References" tab next to the materials, with their size in rows × columns. Here you can:

- open a table in the center of the screen to work with a large table; the "Back to document" button returns you;
- rename a table (the pencil or a double click);
- download a table as CSV;
- delete a table together with its data (on the second press). Before deleting, Lore takes a snapshot of the document named "Before deleting table".

Deleting a whole table from the text — selecting it and pressing Delete, cutting it, or "Delete table" in the cell menu — deletes the table together with its data; one ⌘/Ctrl + Z brings both back. "Delete table" in the menu also takes the "Before deleting table" snapshot first.

If only the marker is damaged (a character of `table:…` was changed), or the table was erased by another person or by undo, the table itself stays: in the list it is marked "no link in text", and the "Insert into document" button puts it back at the cursor.

Copying a table keeps its merged cells: into another Lore document, and into Word, Google Docs or Sheets.

Document snapshots also keep the state of its tables, merges included (see [Snapshots and history](help:history)). The agent sees merged areas and does not write into a cell hidden by a merge.
