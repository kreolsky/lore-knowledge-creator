# Images and embeds

An embed shows the content of another object inside a document: a picture, another document or a reference. It is written as a link starting with `!`: `![caption](address)`. When the cursor is on such a line, you see the entry itself. When the cursor leaves, the content appears in its place.

## Pictures in the text

A picture in the text is always a [reference](help:references). To put a picture into a document:

1. Upload it in the "References" tab, or ask the agent to generate a picture: a generated picture is also saved as a reference of the document.
2. Put the cursor where the picture should go.
3. On the picture's card, press "Insert embed at cursor".

The text gets an entry `![Title](ref:…)`. The size is set in the caption after a vertical bar:

- `![Diagram|300](ref:…)` — 300 pixels wide;
- `![Diagram|50%](ref:…)` — half the page width;
- `![Diagram|300x200](ref:…)` — width and height.

This picture is inserted 300 pixels wide, that is `![Cat orchestra|300](ref:…)`:

![Cat orchestra|300](help-image:cat-orchestra)

And this one at 40% of the page width, `![Cat after a wash|40%](ref:…)`:

![Cat after a wash|40%](help-image:wet-cat)

Both pictures are references of this page: you can see them in the "References" tab while it is open. Put the cursor on a picture's line to see and adjust its entry.

A picture linked from the internet is not shown in a document; only its caption remains. Upload such a picture to "References" first.

## Embedding a document

`![caption](document)` shows the full text of another document inside the page. The embedded text is read-only: edit it in the document itself, and the edits appear everywhere it is embedded. A click on the embedded block's heading opens the source document.

This is handy for shared fragments needed in several places: definitions, stat tables, recurring rules.

How to insert: type `!`, select a caption after it and press ⌘/Ctrl + K, then pick the document. You get `![caption](…)`.

## Embedding a reference

`![caption](ref:…)` works the same way for a reference: a text reference or a recording's transcript is shown inside the page, a picture as a picture, a file as a download card. The "Insert embed at cursor" button on any reference's card inserts such an entry.

## Limitations

- Embedding works one level deep. If an embedded document itself embeds something, you get a plain link there instead of the content. This way a document cannot endlessly embed itself.
- Tables are also placed in the text as an embed `![Title](table:…)`, but they belong to their own document (see [Tables](help:tables)).
- A link without `!` stays a link: a click opens the document, hovering shows a preview.
