---
name: md-folder
description: Use when the user wants a document tree as a zip of Markdown files, or wants a markdown folder imported as documents — Obsidian vault, GitHub docs folder, a markdown archive by link. «выгрузи в zip», «экспорт в markdown», «скачать папкой для Obsidian», «импортируй доки из этого репозитория», «загрузи папку md по ссылке»; export as markdown, import an Obsidian vault.
tools:
  - export_subtree_md
  - import_subtree_md
  - sandbox_bash
  - sandbox_fetch_reference
---

# Markdown folder (zip)

## Export

`export_subtree_md(root_id=<document>)` exports that document with everything under it.
Give the user the returned link as is — it expires soon, so do not save it into a
document. Tell them what the zip leaves out: comments, history, the manual order of
documents (files are alphabetical), and links to documents outside the exported tree.

## Import

`import_subtree_md` takes a .zip from your console workspace (`sandbox_path`). Get it
there first:

- **By link** (a GitHub repository, a published archive) — download it with
  `sandbox_bash` (for GitHub: `https://github.com/<owner>/<repo>/archive/refs/heads/<branch>.zip`).
  Keep only the folder the user asked for: unpack, then zip that folder alone.
- **A .zip already in the project as a file** — `sandbox_fetch_reference(ref_id=…)`.

Before zipping, clean the folder, or the whole import is refused: drop `.svg` files and
remove image links to remote URLs (`![…](https://…)`) — only local raster images
(png, jpg, gif, webp) import. Write the zip inside the workspace, not `/tmp`.

Then `import_subtree_md(parent_id=<where it goes>, sandbox_path=<the zip, relative to the
workspace>)`. Ask where the
tree should land if the user did not say. To undo an import, delete the created subtree.

A zip on the user's own computer cannot reach you: neither a document nor the chat
accepts a .zip upload. Ask for a link, or suggest an external agent connected over MCP,
which imports a local zip itself. Without the console tools (no console on this server)
only export works — say so.
