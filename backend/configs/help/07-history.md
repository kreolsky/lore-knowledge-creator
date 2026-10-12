# Snapshots and history

A snapshot is the saved state of a document at a given moment. Snapshots let you go back to a past version and mark important milestones with a comment. They live in the "Snapshots" tab of the right panel.

## Automatic snapshots

Lore takes snapshots on its own so that nothing gets lost:

- before every edit by the agent;
- on a large deletion or replacement of text;
- when you open a document, if it changed since the last snapshot;
- when another person starts editing the document;
- at the end of your editing session;
- before restoring an old snapshot, so that the restore can be undone.

If the document has not changed since the previous snapshot, no new automatic snapshot is saved (snapshots before the agent's edits are always taken). Old automatic snapshots are gradually thinned out.

## Manual snapshot

Press ⌘/Ctrl + S or "Save snapshot" in the "Snapshots" tab. Add a comment: what changed, what decision was made.

## Viewing, restoring, export

- A click on a snapshot opens it for viewing; hovering shows an excerpt.
- A snapshot can be restored. Before that, Lore saves the current state, so nothing is lost.
- A snapshot's comment can be changed.
- From the "Snapshots" tab the document can be exported to PDF or DOCX.

The tab also shows the document's history: who created the document and who edited it.

## Planned

Comparing two snapshots with the differences highlighted. For now you can compare by opening the snapshots one after another.
