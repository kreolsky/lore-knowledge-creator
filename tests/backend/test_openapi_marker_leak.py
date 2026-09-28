"""OpenAPI marker-leak guard — internal rationale (ARCH/INVARIANT/WHY/DEBT) must not
ship in the public API contract.

# WHY: backend model/route docstrings carry `# ARCH:` / `# INVARIANT:` / `# Why:` markers
# for the project's own discovery index. FastAPI turns those docstrings into OpenAPI
# `description` fields verbatim, so internal rationale bleeds into the published schema.
# export_openapi.py strips marker blocks at export; this test
# pins that the strip works (unit) and that the live schema is clean (integration).
# Plan debt-paydown §A.8.
"""
import copy
import re
import sys
from pathlib import Path

import main

# export_openapi.py is a standalone script (not a package) under backend/scripts/ —
# resolve it from where `main` actually loaded so the path is correct in both the dev
# host and the /app-mounted test container.
_SCRIPTS = Path(main.__file__).resolve().parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import export_openapi  # noqa: E402

strip = export_openapi.strip_markers_from_description
sanitize = export_openapi.sanitize_openapi_schema

_DETECT = re.compile(
    r"^\s*#?\s*(?:ARCH|INVARIANT(?:\([\w-]+\))?|WHY|Why|DEBT):", re.MULTILINE
)


def _leaked_descriptions(schema):
    """Collect (location, snippet) for any description/summary still carrying a marker."""
    found = []

    def walk(node, loc):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("description", "summary") and isinstance(v, str) and _DETECT.search(v):
                    found.append((loc, v.splitlines()[0][:70]))
                walk(v, f"{loc}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{loc}[{i}]")

    walk(schema, "")
    return found


class TestStripper:
    def test_removes_arch_block_with_continuation(self):
        text = "Summary.\n\n# ARCH: foo\n# continuation line\n# more cont\n\nNext para."
        out = strip(text)
        assert "ARCH:" not in out
        assert "continuation line" not in out
        assert "more cont" not in out
        assert "Summary." in out
        assert "Next para." in out

    def test_removes_invariant_tag_and_inline_why(self):
        text = "# INVARIANT(data-loss): never drop the field. Why: list endpoints SELECT *."
        assert "INVARIANT" not in strip(text)
        assert "Why:" not in strip(text)

    def test_removes_adjacent_why_block(self):
        text = "# INVARIANT: foo\n# Why: the reason it holds\nProse after."
        out = strip(text)
        assert "INVARIANT" not in out
        assert "the reason it holds" not in out
        assert "Prose after." in out

    def test_preserves_clean_description(self):
        text = "A normal description with no markers whatsoever."
        assert strip(text) == text

    def test_preserves_non_marker_comment(self):
        # A `#` line that is NOT a marker, and not a continuation of one, stays.
        text = "Summary.\n# a plain note, not a marker\nMore prose."
        out = strip(text)
        assert "a plain note, not a marker" in out

    def test_strips_multiple_contiguous_marker_blocks(self):
        text = "# ARCH: a\n# ARCH: b\n# cont\nKeep this."
        out = strip(text)
        assert "ARCH:" not in out
        assert "cont" not in out
        assert "Keep this." in out

    def test_strips_plain_text_marker_paragraph(self):
        # Marker written WITHOUT a `#` prefix, as a prose paragraph ending at a blank line.
        text = (
            "Summary line.\n\n"
            "INVARIANT: the pointer updates only on intentional open. Why: auxiliary "
            "fetches would clobber it.\n"
            "Continuation of the same paragraph.\n\n"
            "Optimized: access + upsert in fewer queries."
        )
        out = strip(text)
        assert "INVARIANT" not in out
        assert "clobber it" not in out
        assert "Continuation of the same paragraph" not in out
        assert "Summary line." in out
        assert "Optimized: access + upsert in fewer queries." in out

    def test_preserves_inline_marker_mention(self):
        # A mid-sentence `(ARCH: "…")` is NOT a line-start marker — stripping it would
        # break the surrounding sentence, so it must survive.
        text = 'Delegates to the executor (ARCH: "Reads reuse it") so both paths agree.'
        out = strip(text)
        assert '(ARCH: "Reads reuse it")' in out


class TestExportedSchema:
    def test_live_schema_has_no_marker_prefixes_after_sanitize(self):
        schema = export_openapi.build_openapi_schema()
        leaked = _leaked_descriptions(schema)
        assert not leaked, (
            f"marker prefixes leaked into OpenAPI descriptions after sanitize: {leaked[:5]}"
        )

    def test_sanitize_actually_removes_something(self):
        # Non-triviality: the RAW schema currently carries markers, so sanitize must
        # change at least one description. Guards against a no-op stripper passing the
        # test above trivially. deepcopy: FastAPI caches app.openapi_schema, and the
        # clean test above must not mutate what this one reads as "raw".
        raw = copy.deepcopy(main.app.openapi())
        raw_leaked = _leaked_descriptions(raw)
        assert raw_leaked, (
            "raw schema has no marker prefixes — the leak is already gone; this test "
            "no longer proves the stripper does anything (revisit)."
        )
        sanitize(raw)
        assert not _leaked_descriptions(raw), "sanitize left markers behind"
