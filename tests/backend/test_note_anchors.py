"""Note anchors: build/resolve over split non-ASCII text, and the cross-runtime fixtures.

Run as a script to regenerate the pycrdt-built fixture the browser suite resolves:
  docker compose exec -T backend python /tests/backend/test_note_anchors.py \
    > frontend/src/collab/__fixtures__/note-anchors-pycrdt.json
"""

import base64
import json
import sys
from pathlib import Path

import pytest
from pycrdt import Assoc, Doc, Map, StickyIndex, Text

sys.path.insert(0, "/app")
from note_anchors import (  # noqa: E402
    anchor_space,
    build_anchor,
    cp_to_utf16,
    relpos_from_wire,
    relpos_to_wire,
    resolve_anchor,
    utf16_to_cp,
)

YJS_FIXTURE = Path(__file__).parent / "fixtures" / "note_anchors_yjs.json"


def _byte_index(text: Text, cp: int) -> int:
    """pycrdt insert/delete take UTF-8 byte offsets."""
    return len(str(text)[:cp].encode())


def _split_doc() -> tuple[Doc, Text]:
    """Three clients interleave Cyrillic + 😀 edits, so the text spans many items."""
    a = Doc(client_id=7)
    ta = a.get("content", type=Text)
    ta += "абв😀где"
    a.get("tables", type=Map)["t1"] = Map({"rows": [[1, "ж", None, 2.5, True]], "n": "x"})
    b = Doc(client_id=9)
    b.apply_update(a.get_update())
    tb = b.get("content", type=Text)
    tb.insert(_byte_index(tb, 2), "Ж😀x")
    a.apply_update(b.get_update())
    ta.insert(_byte_index(ta, 7), "ЯЯ")
    return a, ta


def _quote(space, rng) -> str:
    return space.text[utf16_to_cp(space.text, rng.start):utf16_to_cp(space.text, rng.end)]


def test_every_range_round_trips_on_split_text():
    doc, text = _split_doc()
    s = str(text)
    space = anchor_space(doc)
    for i in range(len(s)):
        for j in range(i + 1, len(s) + 1):
            rng = resolve_anchor(space, *build_anchor(space, cp_to_utf16(s, i), cp_to_utf16(s, j)))
            assert rng.state == "ok" and _quote(space, rng) == s[i:j], (i, j)


def test_anchor_is_bound_after_and_before():
    doc, text = _split_doc()
    start, end = build_anchor(anchor_space(doc), 1, 3)
    assert StickyIndex.decode(start).assoc is Assoc.AFTER
    assert StickyIndex.decode(end).assoc is Assoc.BEFORE


def test_typing_at_either_edge_does_not_grow_the_anchor():
    doc = Doc(client_id=3)
    text = doc.get("content", type=Text)
    text += "xx😀фраза😀yy"
    s = str(text)
    anchor = build_anchor(anchor_space(doc), cp_to_utf16(s, 3), cp_to_utf16(s, 8))
    text.insert(_byte_index(text, 3), "Л")
    text.insert(_byte_index(text, 9), "Р")
    space = anchor_space(doc)
    assert str(text) == "xx😀ЛфразаР😀yy"
    assert _quote(space, resolve_anchor(space, *anchor)) == "фраза"


def test_partial_delete_shrinks_and_full_delete_orphans():
    doc, text = _split_doc()
    s = str(text)
    anchor = build_anchor(anchor_space(doc), cp_to_utf16(s, 2), cp_to_utf16(s, 6))
    assert s[2:6] == "Ж😀xв"
    del text[_byte_index(text, 2):_byte_index(text, 4)]
    space = anchor_space(doc)
    assert _quote(space, resolve_anchor(space, *anchor)) == "xв"
    del text[_byte_index(text, 2):_byte_index(text, 4)]
    assert resolve_anchor(anchor_space(doc), *anchor).state == "orphan"


def test_anchor_whose_items_have_not_arrived_is_pending():
    doc, _ = _split_doc()
    anchor = build_anchor(anchor_space(doc), 1, 3)
    lagging = Doc()
    lagging.get("content", type=Text)
    assert resolve_anchor(anchor_space(lagging), *anchor).state == "pending"


@pytest.mark.parametrize("start,end", [(3, 3), (5, 2), (0, 99)])
def test_build_refuses_empty_or_out_of_range(start, end):
    doc, _ = _split_doc()
    with pytest.raises(ValueError):
        build_anchor(anchor_space(doc), start, end)


def test_build_refuses_a_split_surrogate_pair():
    doc, text = _split_doc()
    emoji_at = cp_to_utf16(str(text), str(text).index("😀"))
    with pytest.raises(ValueError):
        build_anchor(anchor_space(doc), emoji_at + 1, emoji_at + 3)


def test_utf16_cp_conversion():
    s = "a😀бc"
    assert [cp_to_utf16(s, i) for i in range(5)] == [0, 1, 3, 4, 5]
    assert [utf16_to_cp(s, i) for i in (0, 1, 3, 4, 5)] == [0, 1, 2, 3, 4]


def test_wire_round_trip_and_rejections():
    doc, _ = _split_doc()
    start, _ = build_anchor(anchor_space(doc), 1, 3)
    assert relpos_from_wire(relpos_to_wire(start)) == start
    legacy_json = b'{"type":null,"tname":"content","item":{"client":7,"clock":1},"assoc":0}'
    assert relpos_to_wire(legacy_json) is None
    assert relpos_to_wire(None) is None
    for bad in ("not base64!", base64.b64encode(legacy_json).decode(), ""):
        with pytest.raises(ValueError):
            relpos_from_wire(bad)


def test_yjs_built_anchors_resolve_here():
    """The browser's encodeRelativePosition bytes resolve to the same fragments."""
    fixture = json.loads(YJS_FIXTURE.read_text())
    doc = Doc()
    doc.apply_update(base64.b64decode(fixture["update"]))
    space = anchor_space(doc)
    assert space.text == fixture["text"]
    for case in fixture["anchors"]:
        rng = resolve_anchor(space, relpos_from_wire(case["start"]), relpos_from_wire(case["end"]))
        if case["quote"] is None:
            assert rng.state == "orphan", case
        else:
            assert rng.state == "ok" and _quote(space, rng) == case["quote"], case


DOC_TEXT = "Пролог 😀 — «ключевая фраза» 😀 эпилог"


async def _note_over(client, token, pid, doc_id, fragment: str, **extra):
    """Create a note over `fragment` of the stored doc, anchored server-side."""
    import ydoc_store
    space = anchor_space(await ydoc_store.load(doc_id))
    i = space.text.index(fragment)
    start, end = build_anchor(
        space, cp_to_utf16(space.text, i), cp_to_utf16(space.text, i + len(fragment)),
    )
    body = {
        "project_id": pid, "document_id": doc_id, "is_note": True,
        "anchor_rel_start": relpos_to_wire(start), "anchor_rel_end": relpos_to_wire(end),
        "anchor_quote": fragment, **extra,
    }
    return await client.post("/api/chat/sessions", json=body, cookies={"lore_session": token})


async def _note_rows(client, token, pid, doc_id):
    resp = await client.get(
        "/api/chat/sessions",
        params={"project_id": pid, "document_id": doc_id, "is_note": "true"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return {row["session_id"]: row for row in resp.json()}


@pytest.mark.asyncio
async def test_rest_note_anchor_comes_back_base64_and_resolves(client, admin_user, project_with_doc):
    import ydoc_store
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await ydoc_store.set_content(doc_id, DOC_TEXT, persist=True)
    created = await _note_over(client, token, pid, doc_id, "«ключевая фраза»")
    assert created.status_code == 201, created.text
    row = (await _note_rows(client, token, pid, doc_id))[created.json()["session_id"]]
    assert row["anchor_quote"] == "«ключевая фраза»"
    # A wholesale rewrite around the fragment keeps it anchored.
    await ydoc_store.set_content(doc_id, "Новый 😀 пролог — «ключевая фраза» 😀 и конец", persist=True)
    space = anchor_space(await ydoc_store.load(doc_id))
    rng = resolve_anchor(
        space, relpos_from_wire(row["anchor_rel_start"]), relpos_from_wire(row["anchor_rel_end"]),
    )
    assert rng.state == "ok" and _quote(space, rng) == "«ключевая фраза»"


@pytest.mark.asyncio
async def test_rest_refuses_a_json_anchor_and_a_lone_half(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    json_anchor = '{"type":null,"tname":"content","item":{"client":1,"clock":0},"assoc":0}'
    base = {"project_id": pid, "document_id": doc_id, "is_note": True}
    for extra in (
        {"anchor_rel_start": json_anchor, "anchor_rel_end": json_anchor},
        {"anchor_rel_start": "AAcAAA=="},
    ):
        resp = await client.post(
            "/api/chat/sessions", json={**base, **extra}, cookies={"lore_session": token},
        )
        assert resp.status_code == 422, (extra, resp.text)


@pytest.mark.asyncio
async def test_long_quote_is_capped(client, admin_user, project_with_doc):
    import ydoc_store
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await ydoc_store.set_content(doc_id, DOC_TEXT, persist=True)
    created = await _note_over(client, token, pid, doc_id, "эпилог", anchor_quote="ж" * 600)
    assert created.status_code == 201, created.text
    assert created.json()["anchor_quote"] == "ж" * 500 + "…"


@pytest.mark.asyncio
async def test_legacy_json_anchor_row_lists_as_null(client, admin_user, project_with_doc, test_db):
    """A pre-binary row (JSON text stored as bytes) must not 500 the list."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    created = await client.post(
        "/api/chat/sessions", json={"project_id": pid, "document_id": doc_id, "is_note": True},
        cookies={"lore_session": token},
    )
    sid = created.json()["session_id"]
    legacy = b'{"type":null,"tname":"content","item":{"client":1,"clock":0},"assoc":0}'
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET anchor_rel_start = $b, anchor_rel_end = $b",
        {"id": sid, "b": legacy},
    )
    row = (await _note_rows(client, token, pid, doc_id))[sid]
    assert row["anchor_rel_start"] is None and row["anchor_rel_end"] is None


def pycrdt_fixture() -> dict:
    """Anchors built here, then the text edited around and through them."""
    doc, text = _split_doc()
    s = str(text)
    space = anchor_space(doc)
    ranges = [(0, 3), (2, 6), (3, 4), (7, 9), (9, len(s)), (1, 12)]
    anchors = [build_anchor(space, cp_to_utf16(s, i), cp_to_utf16(s, j)) for i, j in ranges]
    other = Doc(client_id=21)
    other.apply_update(doc.get_update())
    other_text = other.get("content", type=Text)
    other_text.insert(_byte_index(other_text, 7), "Ф")           # at the edge of (7, 9)
    other_text.insert(_byte_index(other_text, 10), "😀")         # at the edge of (7, 9)
    doc.apply_update(other.get_update())
    del text[_byte_index(text, 3):_byte_index(text, 4)]        # (3, 4) is gone
    space = anchor_space(doc)
    cases = []
    for start, end in anchors:
        rng = resolve_anchor(space, start, end)
        cases.append({
            "start": relpos_to_wire(start),
            "end": relpos_to_wire(end),
            "quote": _quote(space, rng) if rng.state == "ok" else None,
        })
    return {
        "update": base64.b64encode(doc.get_update()).decode(),
        "text": space.text,
        "anchors": cases,
    }


def test_pycrdt_fixture_covers_every_state():
    fixture = pycrdt_fixture()
    quotes = [case["quote"] for case in fixture["anchors"]]
    assert None in quotes and "ЯЯ" in quotes  # an orphan, and an edge-typed anchor kept


if __name__ == "__main__":
    print(json.dumps(pycrdt_fixture(), ensure_ascii=False, indent=2))
