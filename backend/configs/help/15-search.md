# Search

## Searching the project

The "Search" tab searches the project's documents and references. There are two modes:

- **Full-text** — search by words. Titles match by the start of a word, text by whole words. Word forms are taken into account for Russian text only (for example, «миром» finds «мира»); in other languages a word must match as written. The "Substring" checkbox switches on a literal search for a fragment in titles and text, without word forms.
- **Semantic** — search by meaning, for when the wording is not remembered. "Memory only" keeps only [memory](help:memory) facts.
- **Chats** — your own chats in this project. Title matches come first (marked "title") and open the chat; content matches open the chat scrolled to the message. Without "In this document", chats of the open document's subtree come first. Chats marked "Unindexed" are found by title only. Opening the Search tab from the Chat tab starts in this mode.

"In this document" limits any mode to the open document and its subtree.

## Find and replace

Search and replace inside the open document. Press ⌘/Ctrl + F while the cursor is in the text: the "Find & Replace" tab opens in the right panel. It is a temporary tab: it disappears when you switch to another one and comes back on ⌘/Ctrl + F. If text was selected beforehand, it goes straight into the search field; pressing ⌘/Ctrl + F again replaces the field's content with the new selection.

The **Find** section:

- the search field; all matches are highlighted in the text;
- the "current / total" counter. "1000+" means there are more than a thousand matches and not all are highlighted;
- "Previous" and "Next" go to the previous and next match. Enter in the field searches forward, ⇧ + Enter backward, and further Enter presses keep stepping;
- "All" selects every match at once, and they can be edited together, like with multiple cursors;
- **Aa** — match case;
- **.\*** — search by regular expression;
- **Whole word** — don't find a word inside another word.

The **Replace** section is visible only to those who can edit the document:

- the replace field;
- "Replace" replaces the current match and moves to the next one;
- "Replace all" replaces every match at once.

In regular-expression mode the replacement can refer to the captured groups: `$1`, `$2`. In split view the search works in the column where the cursor is: in the document or in the reference. A replacement is an ordinary edit; ⌘/Ctrl + Z undoes it.

## Searching through the agent

The agent searches documents, references and memory itself; with the chat-recall [skill](help:skills) it also searches your other chats.
