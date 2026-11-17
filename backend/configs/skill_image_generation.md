---
name: image-generation
description: Use whenever the user asks for a picture — draw, generate, illustrate, a portrait, an illustration for a document, another variant of an image, or a change to one you made.
tools:
  - generate_image
---

# Generating images

`generate_image` returns at once and the picture lands in the working document on its
own in ~10–20s: tell the user it is on its way and finish your turn. A working
document must be open. One call = one picture.

## How many images

`prompt` always describes ONE picture; the quantity lives only in `count`.

| The user wants | Call |
|---|---|
| 3 variants of the same scene | ONE call, `count: 3` |
| 3 different pictures | THREE calls, `count: 1`, a different `prompt` each |

`count` gives variations of a single prompt — it cannot produce different scenes.
An ambiguous ask ("три картинки к главе") almost always means three different
pictures: pick that, and say which you did. "Three images of X, Y and Z" written into
one `prompt` yields one picture holding X, Y and Z — the most common failure here.

## What a prompt must contain

A COMPLETE scene: subject, setting, composition, style, colour, lighting, in plain
prose — no tags, weights or quality boosters. The server sees nothing but this
`prompt`: not the conversation, the document or an earlier image.

So to CHANGE a picture you made, restate the WHOLE scene with the change applied —
never the change alone ("make it darker").

Text that must appear IN the picture (a sign, a title) goes into `prompt` in double
quotes, exactly as it should read and in its language — quoted text is kept verbatim,
not translated.

Keep to two or three subjects. Describe only what IS in the frame: there is no
negative channel, so anything you name you summon.

`orientation`: a character portrait is `portrait`, a landscape or banner is
`landscape`, otherwise `square`.
