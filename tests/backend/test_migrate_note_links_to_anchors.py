"""The note-link migration: `[label](note:ID)` becomes an anchor on the label, the link
syntax leaves the text, the label's items survive — over Cyrillic + 😀 on both sides."""

import secrets

import pytest
from note_anchors import anchor_space, resolve_anchor, utf16_to_cp
from pycrdt import Doc, Text

from migrations.migrate_note_links_to_anchors import (
    migrate_doc_links,
    run,
)


def _doc(content: str) -> Doc:
    doc = Doc(client_id=5)
    doc.get("content", type=Text).insert(0, content)
    return doc


def _quote(doc: Doc, start: bytes, end: bytes) -> str:
    space = anchor_space(doc)
    rng = resolve_anchor(space, start, end)
    assert rng.state == "ok", rng
    return space.text[utf16_to_cp(space.text, rng.start):utf16_to_cp(space.text, rng.end)]


def test_link_becomes_an_anchor_on_its_label():
    doc = _doc("до 😀 [фраза 😀 тут](note:n1) после 😀")
    result = migrate_doc_links(doc, "d1", {"n1": "d1"})
    assert str(doc.get("content", type=Text)) == "до 😀 фраза 😀 тут после 😀"
    anchor = result.anchors["n1"]
    assert anchor.quote == "фраза 😀 тут"
    assert _quote(doc, anchor.rel_start, anchor.rel_end) == "фраза 😀 тут"


def test_the_update_carries_the_removal_to_a_peer_and_the_anchor_resolves_there():
    doc = _doc("аб [[Ж😀]](note:n1) вг")
    peer = Doc()
    peer.apply_update(doc.get_update())
    result = migrate_doc_links(doc, "d1", {"n1": "d1"})
    peer.apply_update(result.update)
    assert str(peer.get("content", type=Text)) == "аб Ж😀 вг"
    anchor = result.anchors["n1"]
    assert _quote(peer, anchor.rel_start, anchor.rel_end) == "Ж😀"


def test_typing_at_the_label_edges_does_not_grow_the_migrated_anchor():
    doc = _doc("x[ёж](note:n1)y")
    anchor = migrate_doc_links(doc, "d1", {"n1": "d1"}).anchors["n1"]
    text = doc.get("content", type=Text)
    assert str(text) == "xёжy"
    text.insert(1, "😀")                               # before «ё» (byte 1)
    text.insert(len("x😀ёж".encode()), "Ф")            # after «ж»
    assert str(text) == "x😀ёжФy"
    assert _quote(doc, anchor.rel_start, anchor.rel_end) == "ёж"


def test_links_without_a_live_host_note_are_stripped_to_their_label():
    doc = _doc("[чужая](note:other) [нет](note:gone) [](note:n1) [😀](note:n1) [второй](note:n1)")
    result = migrate_doc_links(doc, "d1", {"n1": "d1", "other": "d2"})
    assert str(doc.get("content", type=Text)) == "чужая нет  😀 второй"
    assert set(result.anchors) == {"n1"}
    assert result.anchors["n1"].quote == "😀"
    assert result.stripped == 4


def test_a_document_with_no_link_is_untouched():
    doc = _doc("ничего 😀 [ссылка](doc:abc)")
    before = doc.get_update()
    result = migrate_doc_links(doc, "d1", {"n1": "d1"})
    assert result.update is None and result.anchors == {}
    assert doc.get_update() == before


def test_a_long_label_is_capped_in_the_quote():
    label = "ж" * 600
    result = migrate_doc_links(_doc(f"[{label}](note:n1)"), "d1", {"n1": "d1"})
    assert result.anchors["n1"].quote == "ж" * 500 + "…"


@pytest.mark.asyncio
async def test_run_writes_the_note_row_before_publishing_the_removal(
    test_db, project_with_doc,
):
    import ydoc_store
    from db import create_record, fetch_one, get_db

    pid, host_id, admin_uid = project_with_doc
    doc_id = f"nl-doc-{secrets.token_hex(4)}"
    note_id = f"nl-note-{secrets.token_hex(4)}"
    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": host_id, "title": "Host", "content": "",
        "path": "host.md", "is_index": False, "is_reference": False,
    })
    await create_record("chat_sessions", note_id, {
        "project_id": pid, "document_id": doc_id, "user_id": admin_uid, "is_note": True,
    })
    await ydoc_store.set_content(doc_id, f"Ж😀 [метка 😀](note:{note_id}) ё", persist=True)

    seen_at_publish: list[dict | None] = []

    async def recorder(entity_id, update, *, append=True):
        seen_at_publish.append(await fetch_one("chat_sessions", note_id))
        await ydoc_store.publish_doc_update(entity_id, update, append=append)

    db = await get_db()
    await run(db, dry_run=True, publish=recorder)
    assert seen_at_publish == []
    assert (await fetch_one("chat_sessions", note_id)).get("anchor_rel_start") is None

    await run(db, publish=recorder)
    assert seen_at_publish and seen_at_publish[0].get("anchor_rel_start")
    loaded = await ydoc_store.load(doc_id)
    assert str(ydoc_store.get_text(loaded)) == "Ж😀 метка 😀 ё"
    row = await fetch_one("chat_sessions", note_id)
    assert row["anchor_quote"] == "метка 😀"
    assert _quote(loaded, row["anchor_rel_start"], row["anchor_rel_end"]) == "метка 😀"
    # The read model (search, embeddings, every documents.content reader) drops the
    # link too, without waiting for the document's next compaction.
    assert (await fetch_one("documents", doc_id))["content"] == "Ж😀 метка 😀 ё"

    # Idempotent: the text holds no link any more, nothing is published again.
    await run(db, publish=recorder)
    assert len(seen_at_publish) == 1
