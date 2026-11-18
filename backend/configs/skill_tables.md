---
name: tables
description: Use when the user wants to change an existing table — edit cells, add rows or columns — or to create a named table or one placed at the end of a specific section. «таблица», «добавь строку», «добавь столбец», «поправь ячейку», «заполни таблицу», «создай таблицу в разделе»; table, add a row, add a column, edit a cell.
tools:
  - create_table
  - edit_table_cell
  - add_table_rows
  - add_table_column
---

# Tables

A table in a document is an editable table object, not text: change it with these tools,
never by rewriting its pipe lines through `edit_document`.

## Read first

Call `read_document` before every table call. It gives the `table_id`, the live column
names and the column count — every tool below addresses the table by those, and a
guessed width or column name is rejected.

## Which tool

- **Cells** — `edit_table_cell` takes all edits as one atomic batch: `{table_id, row,
  column, old_value, new_value}` per cell, a single cell is a list of one. `column` is the
  column's name, `old_value` is what the cell holds now.
- **Rows** — `add_table_rows` appends to the bottom. Each row is a list of cell values in
  column order; a shorter row pads empty, a longer one is refused.
- **A column** — `add_table_column` inserts ONE column: `header` is its row-0 cell,
  `values` the data cells (padded empty), `at_index` the position (default: the end).
- **A new table** — a pipe table written into the body with `edit_document` already
  becomes a table object on its own lines; that is enough for a plain table. Use
  `create_table` only to give the table a name, or to place it at the end of one heading
  section (`section` = the exact heading text from `read_document`). `rows[0]` is the
  header.

Every call is reversible from the History panel. In confirmation mode it waits for the
user's approval; a `rejected` result carries their feedback — act on it, do not repeat
the same call.
