"""GFM serialization / parsing for the table block object (Order step 1).

Pure-logic focus: the table model (rows = list[list[str]] cell bodies) round-trips
to a GFM table and back. Escaping is the real work and is pinned here — `|` and
intra-cell `\n` are where GFM serializers break.

See `.kilo/plans/table-block-object.md` (Storage / Markdown export).
"""
from table_serialize import parse_gfm_table, serialize_table

# ── serialize_table: model → GFM ──────────────────────────────────────────────


def test_basic_2x2():
    md = serialize_table([["a", "b"], ["c", "d"]])
    assert md == "| a | b |\n| --- | --- |\n| c | d |"


def test_1x1():
    md = serialize_table([["only"]])
    assert md == "| only |\n| --- |"


def test_pipe_in_cell_is_escaped():
    md = serialize_table([["a|b", "c"], ["d", "e"]])
    # The literal pipe must be backslash-escaped so it is not read as a column sep.
    assert "a\\|b" in md
    # And the cell stays in ONE column (the separator row has 2 columns).
    assert md.splitlines()[1] == "| --- | --- |"


def test_newline_in_cell_becomes_br():
    md = serialize_table([["line1\nline2", "x"], ["y", "z"]])
    assert "line1<br>line2" in md
    # No raw newline leaks into the cell (would break the row).
    assert md.count("\n") == 2  # 3 rows → 2 separators


def test_empty_cells():
    md = serialize_table([["", ""], ["", ""]])
    assert md == "|  |  |\n| --- | --- |\n|  |  |"


def test_leading_trailing_spaces_preserved_via_escape():
    # GFM strips surrounding cell whitespace; significant spaces must survive a
    # round-trip. We don't assert the exact wire form here, only the round-trip.
    rows = [["  pad  ", "b"], ["c", "d"]]
    assert parse_gfm_table(serialize_table(rows)) == rows


def test_ragged_rows_padded_to_widest():
    md = serialize_table([["a"], ["b", "c", "d"]])
    lines = md.splitlines()
    # Header padded to 3 columns; separator has 3 columns.
    assert lines[0] == "| a |  |  |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| b | c | d |"


def test_empty_model_returns_empty_string():
    assert serialize_table([]) == ""


# ── parse_gfm_table: GFM → model (import / round-trip) ─────────────────────────


def test_parse_basic():
    rows = parse_gfm_table("| a | b |\n| --- | --- |\n| c | d |")
    assert rows == [["a", "b"], ["c", "d"]]


def test_parse_unescapes_pipe():
    rows = parse_gfm_table("| a\\|b | c |\n| --- | --- |\n| d | e |")
    assert rows == [["a|b", "c"], ["d", "e"]]


def test_parse_br_back_to_newline():
    rows = parse_gfm_table("| line1<br>line2 | x |\n| --- | --- |\n| y | z |")
    assert rows == [["line1\nline2", "x"], ["y", "z"]]


def test_round_trip_with_specials():
    rows = [["a|b", "c\nd"], ["", "  e  "]]
    assert parse_gfm_table(serialize_table(rows)) == rows


def test_parse_does_not_drop_all_empty_data_row():
    # An all-empty data row (`|   |   |`) is a DATA row, not the separator — must survive
    # (the bug dropped it entirely). Assert row count + boundary rows, not exact empty-cell
    # whitespace (a separate stripping concern).
    rows = parse_gfm_table("| a | b |\n| --- | --- |\n|   |   |\n| c | d |")
    assert len(rows) == 3  # header + empty row + last row — NOT 2
    assert rows[0] == ["a", "b"]
    assert rows[2] == ["c", "d"]


def test_parse_unescapes_br_variants_case_insensitive():
    # Parity with the frontend importer: <br>, <br/>, <br />, any case. Leading <BR/> → \n.
    rows = parse_gfm_table("| h |\n| --- |\n| <BR/>x<br />y<br>z |")
    assert rows == [["h"], ["\nx\ny\nz"]]
