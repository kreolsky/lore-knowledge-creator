"""D9 — one door: an `is_memory` document is created ONLY through the apply path.

Plan `.kilo/plans/1786230000000-memory-flatten-to-facts.md` D9. abot2's
`test_fact_journal.py` reads the source and fails when a new code path creates a fact
directly; three paths had bypassed the door, none maliciously. Our equivalent: a test
that scans the backend source for any WRITE that sets `is_memory` to true, and asserts
the only such site is `memory/_apply_resolution._create_fact`.

A fact created anywhere else cannot be reconstructed from the reference journal (D8),
and the recovery promise the whole model rests on silently stops holding — invisible
until the day it is needed.
"""

import re
from pathlib import Path

_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")


def _backend_root() -> Path:
    # The backend source root is the directory that contains the `memory/` package.
    # In the test container the source lives at /app; on the host it is <repo>/backend.
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "backend", Path("/app"), here.parents[1], here.parents[2]):
        if (cand / "memory" / "__init__.py").is_file():
            return cand
    raise RuntimeError("could not locate the backend source tree")


def _writes_is_memory_true(text: str) -> bool:
    """True when a line WRITES `is_memory` to a truthy value (not a WHERE read)."""
    t = text.strip()
    # SQL UPDATE writes: `SET ... is_memory = true`. WHERE/AND reads are excluded by
    # requiring `SET` on the same line.
    if "is_memory = true" in t and "SET" in t:
        return True
    # Dict-literal writes to create_record / UPDATE content payloads.
    if '"is_memory": True' in t or "'is_memory': True" in t:
        return True
    return False


def _enclosing_function(lines: list[str], idx: int) -> str | None:
    """The name of the function enclosing line `idx` (0-based), by scanning back to the
    nearest `def`. Pins that the one door is `_create_fact` the FUNCTION, not just its
    file: a second fact-minting function in the same module would pass a file-level
    exemption while bypassing the journal."""
    for j in range(idx, -1, -1):
        m = _DEF.match(lines[j])
        if m:
            return m.group(1)
    return None


def test_is_memory_is_written_only_through_create_fact():
    """The ONLY backend source site that writes `is_memory = true` is the apply path's
    `_create_fact` in `memory/_apply_resolution.py` — exactly one site, and that one is
    inside `_create_fact` the function (not merely its file)."""
    root = _backend_root()
    sites: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if _writes_is_memory_true(line):
                fn = _enclosing_function(lines, i - 1)
                sites.append(f"{path.relative_to(root)}:{i} ({fn})")
    assert len(sites) == 1 and sites[0].endswith("(_create_fact)"), (
        "is_memory=true must be written at exactly ONE site — the apply path's "
        "_create_fact. Found:\n" + "\n".join(sites)
    )
    assert sites[0].startswith("memory/_apply_resolution.py"), (
        "the one is_memory=true write moved out of the apply path:\n" + sites[0]
    )
