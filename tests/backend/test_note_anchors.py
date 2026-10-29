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
from pycrdt import Doc, Map, Text

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


def test_anchor_whose_items_have_not_arrived_is_pending():
    doc, _ = _split_doc()
    anchor = build_anchor(anchor_space(doc), 1, 3)
    lagging = Doc()
    lagging.get("content", type=Text)
    assert resolve_anchor(anchor_space(lagging), *anchor).state == "pending"


def test_build_refuses_a_split_surrogate_pair():
    doc, text = _split_doc()
    emoji_at = cp_to_utf16(str(text), str(text).index("😀"))
    with pytest.raises(ValueError):
        build_anchor(anchor_space(doc), emoji_at + 1, emoji_at + 3)


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


async def _note_rows(client, token, pid, doc_id):
    resp = await client.get(
        "/api/chat/sessions",
        params={"project_id": pid, "document_id": doc_id, "is_note": "true"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return {row["session_id"]: row for row in resp.json()}


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


if __name__ == "__main__":
    print(json.dumps(pycrdt_fixture(), ensure_ascii=False, indent=2))
