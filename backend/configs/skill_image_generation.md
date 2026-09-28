---
name: image-generation
description: Use whenever the user asks for a picture — draw, generate, illustrate, render, "make an image of", a portrait, an illustration for a document, another variant of an image, or a change to one you already made. Activates the generate_image tool.
tools:
  - generate_image
---

# Generating images

`generate_image` renders a picture from a prose description and attaches it to the
chat's working document. It returns `{status:'generating', run_id, doc_id}`
immediately; the image arrives on its own in ~10–20s. Tell the user it is on its way
and finish your turn — the picture lands in the document without you. A second call
renders a second picture, so call it once per picture you want. A working document
must be open.

## How many images

The number the user asked for NEVER goes into `prompt`. `prompt` always describes ONE
picture; the quantity lives only in `count`.

| The user wants | Call |
|---|---|
| 3 variants of the same scene | ONE call, `count: 3` |
| 3 different pictures | THREE calls, `count: 1`, a different `prompt` each |

`count` renders N variations of a SINGLE prompt in one batch — same subject, different
noise. It cannot produce different scenes. When the ask is ambiguous ("три картинки к
главе"), it is almost always three different pictures: pick that, and say which you
did.

Writing "three images of X, Y and Z" into `prompt` produces one picture containing X,
Y and Z. That is the single most common failure here.

## What a prompt must contain

`prompt` is a COMPLETE, self-contained description: subject, setting, composition,
style, colour, lighting. Plain prose — not tags, not weights, not quality boosters.
The server rephrases it for the image model and sees NOTHING else: not the
conversation, not the document, not any earlier image.

So when the user asks to CHANGE a picture you already made, restate the WHOLE scene
with the change applied. Never send the change alone ("make it darker") — there is
nothing for it to modify.

Text that must appear IN the picture (a sign, a title, a label) goes into `prompt`
in double quotes, exactly as it should read and in the language it should be written
in — the server keeps quoted text verbatim and does not translate it.

Keep to two or three distinct subjects. Describe only what IS in the frame: there is
no negative channel, so anything you name is something you summon.

`orientation` is `square` (default), `portrait` or `landscape`. Pick it from the use:
a character portrait is `portrait`, a landscape or a banner is `landscape`.
