"""Dump the FastAPI OpenAPI schema to openapi.json (no server / no DB needed).

Builds `main:app` and calls `app.openapi()`. The FastAPI lifespan does NOT run on
import (no DB/Redis connection is opened), so this works anywhere the backend deps
are installed — it only reads the registered routes + their response_model / request
Pydantic models.

This is the source artifact of the CI backend surface drift gate: `backend-lint`
regenerates it and `git diff --exit-code openapi.json` catches an un-committed
OpenAPI surface change (new/removed/renamed route, changed response_model /
request body). Without it, a backend surface change slips through with the
tracked openapi.json silently stale.

Output: repo-root `openapi.json` (the path backend-lint's diff reads). Override
with `--out PATH` or the `OPENAPI_OUT` env var (CI sets it so the in-container
write lands where docker-cp expects).
"""
# SYSTEM: openapi-export — FastAPI OpenAPI schema dump (source of the tracked openapi.json surface drift gate)

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

# backend/ on sys.path so `import main` resolves regardless of cwd
_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

# Config loads at `import main` time and _require_env-s a handful of vars
# (LORE_SECRET_KEY, STORAGE_PATH, MAX_*_SIZE_MB, REDIS_URL — see backend/config.py,
# the canonical list). openapi export never starts the server or opens DB/Redis (the
# FastAPI lifespan does not run on import), so load-bearing dummies are enough to
# satisfy import; real env wins via setdefault when present (dev / prod).
for _k, _v in {
    "LORE_SECRET_KEY": "ci-openapi-export",
    "STORAGE_PATH": "/tmp/lore-storage",
    "MAX_AUDIO_SIZE_MB": "1",
    "MAX_IMAGE_SIZE_MB": "1",
    "MAX_MARKDOWN_SIZE_MB": "1",
    "REDIS_URL": "redis://localhost:6379/0",
}.items():
    # Not setdefault: CI loads .env into the container where these keys are PRESENT
    # but EMPTY (e.g. LORE_SECRET_KEY=) — setdefault keeps the empty value and
    # _require_env still raises. Treat empty the same as absent.
    if not os.environ.get(_k):
        os.environ[_k] = _v

# Imported after the sys.path tweak above so `main` resolves. Run only in an env with
# the backend deps installed (CI / dev containers).
from main import app  # noqa: E402

# Marker-prefix at the start of a logical line in a docstring-as-description. Internal
# rationale (ARCH/INVARIANT/WHY/DEBT markers, plus the `Why:` adjacency that carries an
# INVARIANT's reason) is written into backend docstrings for the project's own discovery
# index; FastAPI turns those docstrings into OpenAPI `description` fields verbatim, leaking
# the rationale into the public contract. Two spellings exist in source: `# MARKER:` comment
# blocks and plain-text `MARKER:` paragraphs. Both are stripped AT EXPORT (never by editing
# the docstrings — the markers stay load-bearing in source). The leading `#?` makes one
# pattern match both; re.MULTILINE so it also serves as a leak detector over a full string.
# Inline mentions (e.g. mid-sentence `(ARCH: "…"`) are NOT at a line start, so they are
# preserved — stripping them would break the sentence around them.
_MARKER_LINE = re.compile(
    r"^\s*#?\s*(?:ARCH|INVARIANT(?:\([\w-]+\))?|WHY|Why|DEBT):", re.MULTILINE
)
_COMMENT_LINE = re.compile(r"^\s*#")

_DESC_KEYS = ("description", "summary")


def strip_markers_from_description(text: str) -> str:
    """Remove marker blocks from a description, returning the public-facing prose.

    Block extent depends on spelling:
      - `# MARKER:` comment block → the marker line + contiguous `#`-comment lines.
      - plain `MARKER:` paragraph  → the marker line + lines until a blank line.
    Both terminate at a paragraph boundary; blank runs left behind are collapsed.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _MARKER_LINE.match(line):
            out.append(line)
            i += 1
            continue
        hashed = _COMMENT_LINE.match(line) is not None
        i += 1  # drop the marker line itself
        if hashed:
            while i < len(lines) and _COMMENT_LINE.match(lines[i]):
                i += 1  # drop contiguous comment continuation
        else:
            while i < len(lines) and lines[i].strip() != "":
                i += 1  # drop the rest of the plain-text marker paragraph
        # leave any blank line for the collapse step
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def sanitize_openapi_schema(schema) -> None:
    """Recursively strip marker blocks from every description/summary in the schema (in place)."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in _DESC_KEYS and isinstance(value, str):
                schema[key] = strip_markers_from_description(value)
            else:
                sanitize_openapi_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            sanitize_openapi_schema(item)


def build_openapi_schema():
    """Return the FastAPI OpenAPI schema with internal marker rationale stripped (codegen source).

    deepcopy first: FastAPI caches `app.openapi_schema` after the first call, and
    `sanitize_openapi_schema` mutates in place — without the copy, sanitizing once would
    poison the cache so every later caller (and a second test) sees an already-clean schema.
    """
    schema = copy.deepcopy(app.openapi())
    sanitize_openapi_schema(schema)
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default=os.environ.get("OPENAPI_OUT"),
        help="output path (default: <repo>/openapi.json, or $OPENAPI_OUT)",
    )
    args = parser.parse_args()

    schema = build_openapi_schema()
    out = Path(args.out) if args.out else _BACKEND_DIR.parent / "openapi.json"
    # sort_keys=True for byte-stable output — the CI drift gate `git diff --exit-code`
    # must not false-positive on nondeterministic key ordering.
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    n_paths = len(schema.get("paths", {}))
    n_schemas = len(schema.get("components", {}).get("schemas", {}))
    print(f"wrote {out} — {n_paths} paths, {n_schemas} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
