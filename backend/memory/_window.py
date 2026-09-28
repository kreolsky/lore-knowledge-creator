"""Windowed portions — slice ONE reference's content into N windows on the way OUT.

The window belongs to the TOOL, not the DATA: `content` is sliced here on serve and no
reference row is ever modified (the rejected first cut of this experiment sliced material
into separate reference documents; that would make the derived slices the thing on record,
but the knowledge base must rebuild from immutable references).

The slicing is a byte-for-byte port of the bench that produced the measured fact yield
(the bench is retired; its oracle survives inlined in
`tests/backend/test_window_portions.py::test_slice_windows_equals_the_bench_oracle`).
count = round(len/window), seams placed evenly, each interior seam widened by ±overlap//2
and snapped to the nearest whitespace within ±120 chars. The production code MUST stay
equal to that oracle, or the acceptance it encodes (a real run reproducing the 2.2x fact
yield) silently diverges.

A reference that fits one window is served whole (passthrough — the change is invisible
for short material, and a single-window stamp consumes the reference in one step exactly
as before).
"""
from __future__ import annotations

# Window 12 000 chars, overlap 2 500 — from the plan's measured table (the 12k arm:
# 2.2x facts, 7% spread, flat precision vs the whole-reference arm).
WINDOW_CHARS = 12_000
OVERLAP_CHARS = 2_500

# _snap searches ±this many chars for the nearest whitespace boundary.
_SNAP_RADIUS = 120


def window_count(content_len: int, *, window: int = WINDOW_CHARS) -> int:
    """Number of windows for `content_len` chars — `round(len/window)`, floor 1.

    Pure in the LENGTH only (the count is chosen before seams are snapped), so it agrees
    with `len(slice_windows(text))` for any text of that length. 1 for anything that fits
    one window (passthrough).
    """
    return max(1, round(content_len / window))


def _snap(text: str, pos: int) -> int:
    """Move a cut to the nearest whitespace within ±_SNAP_RADIUS chars — never mid-word.

    Returns the index JUST PAST the whitespace (the cut lands after a space/newline), or
    `pos` unchanged when no whitespace is in range (the no-whitespace fallback; real
    material always has a boundary nearby). Verbatim from the bench oracle.
    """
    if pos <= 0:
        return 0
    if pos >= len(text):
        return len(text)
    for d in range(_SNAP_RADIUS + 1):
        for p in (pos - d, pos + d):
            if 0 < p < len(text) and text[p].isspace():
                return p + 1
    return pos


def slice_windows(
    text: str, *, window: int = WINDOW_CHARS, overlap: int = OVERLAP_CHARS,
) -> list[tuple[int, int, str]]:
    """Cut `text` into windows of about `window`, sharing `overlap` at every seam.

    Returns `[(from, to, substring), ...]`. The count is chosen first
    (`round(len/window)`) so there is never a runt tail, then the seams are placed evenly
    and each interior boundary is widened by ±overlap//2 (the overlap) and snapped to
    whitespace. `from`/`to` are offsets into `text`; window 0 starts at 0, the last ends
    at `len(text)`, and interior seams overlap (a fact straddling a seam meets its twin
    in both windows → a merge candidate, never a forked duplicate).
    """
    n = max(1, round(len(text) / window))
    if n == 1:
        return [(0, len(text), text)]
    base, half = len(text) / n, overlap // 2
    out: list[tuple[int, int, str]] = []
    for i in range(n):
        a = _snap(text, round(i * base) - half) if i else 0
        b = _snap(text, round((i + 1) * base) + half) if i < n - 1 else len(text)
        out.append((a, b, text[a:b]))
    return out


__all__ = ["OVERLAP_CHARS", "WINDOW_CHARS", "slice_windows", "window_count"]
