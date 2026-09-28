---
name: sandbox
description: Use when the task needs a real Linux console — running code or scripts, calculations over data, installing a package, or reading the raw BYTES of an uploaded image/audio/.docx. Activates sandbox_bash, sandbox_fetch_reference, sandbox_fetch_skill and sandbox_run_status.
tools:
  - sandbox_bash
  - sandbox_fetch_reference
  - sandbox_fetch_skill
  - sandbox_run_status
---

# Sandbox console

A Linux workspace with Python, pip, gcc, git and pandas/numpy, plus internet access.
Files persist across calls and chat sessions; each call is a separate shell.

## When this is the wrong tool

Most work in Lore is document work, and the console cannot reach Lore or any internal
service. Reach for it only when something must actually be COMPUTED or a file's BYTES
must be read.

An uploaded audio file, `.docx` or image already carries its text: the import
pipeline transcribed or converted it, and `read_document` on the reference gives you
that text. That is the text to work from, and it is the canon — the reference's
document content IS the reference, the binary is only the file it arrived as.

When that text looks wrong or comes back empty, `reprocess_reference` re-runs the
pipeline that produced it; when that is not the answer, say so and ask the user.

INVARIANT: a reference's text comes from the pipeline, never from this console. Why:
rebuilding it here — ASR over the audio, OCR over the image, a converter over the
`.docx` — is slower and worse than the text you already have, and produced the
incident this rule replaces. Fetch the binary when the user names the medium ("the
audio", "the picture") or the operation, and code here has to read the actual bytes.

## Getting work in and out

- IN: `sandbox_fetch_reference` copies an uploaded reference's binary into the
  workspace at a deterministic path.
- IN: `sandbox_fetch_skill` materializes a project skill's files (`SKILL.md` +
  its `scripts/…` and `references/…` children) into the workspace at a
  deterministic directory; run a skill's scripts from the returned path.
- OUT: pass the file's workspace path as `sandbox_path` to `import_file` (which stays
  available without this skill). Never base64 a file through the request.

## Work that outlives the turn

A command over the 300-second foreground cap goes with `detach: true`: it returns
`{status:"running", run_id}` at once and keeps going after the call. Poll it with
`sandbox_run_status` — read-only, so it does not take the console and several runs can
be in flight. Do NOT busy-wait in this turn; an unfinished run is collected in the
NEXT one. The workspace is ONE shared file tree, so never launch two detached runs
that would fight over the same files.

## Working habits

- Chain steps with `&&` in ONE command, or write a script and run it — the working
  directory, environment and Python state do not survive between calls.
- A leftover file from earlier work can make a run non-reproducible. If the
  environment seems wedged, ask the user to restart the sandbox.
- You are a non-root user: no sudo, no apt-get. Install with `pip install X`.
