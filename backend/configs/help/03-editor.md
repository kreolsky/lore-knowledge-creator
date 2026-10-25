# Editor

A document is Markdown with live rendering: the markup hides, and headings, lists, tables, pictures, formulas and diagrams are shown in their finished form. Every edit is saved automatically, and several people can edit a document at the same time.

## Selection toolbar

When you select text, a pop-up toolbar appears:

- text style: plain text or heading 1–4;
- bold, italic, strikethrough;
- lists: bulleted, numbered, checkboxes;
- quote, inline code, code block;
- highlight color;
- link;
- note (a discussion attached to the selected text);
- "Create reference from selection" — the selected fragment becomes a reference of the document;
- "Work with selection" — hands the selected fragment to the agent in the chat.

Almost everything on the toolbar is also available from the keyboard.

## Keyboard shortcuts

⌘ on macOS, Ctrl on Windows and Linux.

| Action | Keys |
|---|---|
| Bold | ⌘ B |
| Italic | ⌘ I |
| Strikethrough | ⇧ ⌘ X |
| Inline code | ⌘ E |
| Code block | ⌥ ⌘ E |
| Heading 1–4 | ⌥ ⌘ 1 … ⌥ ⌘ 4 |
| Clear formatting | ⌥ ⌘ 0 |
| Bulleted list | ⇧ ⌘ 0 |
| Numbered list | ⇧ ⌘ 8 |
| Checkboxes | ⇧ ⌘ 9 |
| Indent | ⌘ ] or Tab |
| Outdent | ⌘ [ or ⇧ Tab |
| Link | ⌘ K |
| Note | ⌘ M |
| Highlight color | ⌘ O |
| Insert table | ⇧ ⌘ T |
| Find and replace | ⌘ F |
| Work with selection (agent) | ⌘ J |
| Voice input | ⌘ D |
| Save snapshot | ⌘ S |
| Show raw markup or return to rendering | ⌘ / |
| Hide or show the left panel | ⌘ \ |
| Send a chat message | ⌘ Enter |

## Highlight colors

Highlight colors mark fragments of text: orange, blue, green, purple, yellow. They are handy for marking categories such as "decided", "open question", "outdated". The agent understands these marks and uses the same colors.

## Markup shown in finished form

- Headings, lists, quotes, checkboxes (clickable for those who can edit the document), bold, italic, strikethrough.
- Code blocks with syntax highlighting and a copy button.
- Formulas: `$...$` inline, `$$...$$` as a separate block (LaTeX).
- Diagrams: a code block with the `mermaid` language.
- Tables are separate objects, see [Tables](help:tables).
- Pictures and embedded documents, see [Images and embeds](help:embeds).

When the cursor is on a line, that line's markup becomes visible and editable. ⌘/Ctrl + / shows the whole document as raw Markdown.

## Links in the text

A click on a link opens the document, reference or note; hovering shows a preview. A link is set with ⌘/Ctrl + K over selected text (see [Documents and the graph](help:documents-graph)).

## Pasting from the clipboard

⌘/Ctrl + V converts formatting from Word, Google Docs or a web page into Markdown, and a copied table becomes a [Lore table](help:tables). Text copied from Lore itself pastes without loss, with all its links. ⌘/Ctrl + ⇧ + V pastes plain text without formatting.

## Files in a document

A text file or a Word file can be dragged straight into a document: Lore offers to insert its content below the line where you dropped it. Pictures, recordings and PDFs are not inserted into the text: they are uploaded in the "References" tab (see [References](help:references)).
