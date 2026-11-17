---
name: sandbox
description: Use when the task needs a real Linux console — running code, calculations over data, installing a package, reading the raw bytes of an uploaded file, or rewriting an existing html page in place (redesign, restructure, rework its script). «переделай страницу», «перерисуй дизайн страницы»; redesign this page.
tools:
  - sandbox_bash
  - sandbox_fetch_reference
  - sandbox_fetch_skill
  - sandbox_run_status
  - sandbox_run_kill
  - sandbox_view_image
  - sandbox_fetch_artifact
  - replace_artifact_page
---

# Sandbox console

A Linux workspace with Python, pip, gcc, git and pandas/numpy, plus internet access.
Files persist across calls and chat sessions; each call is a separate shell.

## When this is the wrong tool

Most work in Lore is document work, and the console cannot reach Lore. Use it only
when something must be COMPUTED or a file's BYTES must be read.

INVARIANT: a reference's text comes from the pipeline, never from this console. Why:
an uploaded audio file, `.docx` or image was already transcribed or converted on
import — `read_document` on the reference gives that text, and it is the canon;
rebuilding it here (ASR, OCR, a converter) is slower and worse. If that text looks
wrong or is empty, `reprocess_reference` re-runs the pipeline; if that is not the
answer, say so and ask the user. Fetch the binary only when the user names the medium
("the audio", "the picture") or an operation that has to read the actual bytes.

## Getting work in and out

- IN: `sandbox_fetch_reference` copies an uploaded reference's binary into the
  workspace at a deterministic path.
- IN: `sandbox_fetch_skill` materializes a project skill's files (`SKILL.md`,
  `scripts/…`, `references/…`) into a deterministic directory; run its scripts from
  the returned path.
- OUT: the file's workspace path as `sandbox_path` to `import_file`. Never base64 a
  file through the request.
- An existing html page: `sandbox_fetch_artifact` → edit the file here →
  `replace_artifact_page` on the same path. The page keeps its id, links and History.

## Long runs

Over the 300-second cap → `detach: true`, then `sandbox_run_status`. Do not busy-wait:
an unfinished run is collected in the NEXT turn. Never run two detached jobs over the
same files.
