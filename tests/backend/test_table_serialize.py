"""GFM serialization / parsing for the table block object (Order step 1).

Pure-logic focus: the table model (rows = list[list[str]] cell bodies) round-trips
to a GFM table and back. Escaping is the real work and is pinned here — `|` and
intra-cell `\n` are where GFM serializers break.

See `.kilo/plans/table-block-object.md` (Storage / Markdown export).
"""
from helpers import parse_gfm_table

from table_serialize import serialize_table

# ── serialize_table: model → GFM ──────────────────────────────────────────────


def test_empty_model_returns_empty_string():
    assert serialize_table([]) == ""


# ── parse_gfm_table: GFM → model (import / round-trip) ─────────────────────────


def test_parse_does_not_drop_all_empty_data_row():
    # An all-empty data row (`|   |   |`) is a DATA row, not the separator — must survive
    # (the bug dropped it entirely). Assert row count + boundary rows, not exact empty-cell
    # whitespace (a separate stripping concern).
    rows = parse_gfm_table("| a | b |\n| --- | --- |\n|   |   |\n| c | d |")
    assert len(rows) == 3  # header + empty row + last row — NOT 2
    assert rows[0] == ["a", "b"]
    assert rows[2] == ["c", "d"]
