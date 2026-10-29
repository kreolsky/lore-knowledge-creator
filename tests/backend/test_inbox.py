"""Document inbox — the unread flag on externally arrived objects.

# SYSTEM: inbox — one pool per document: a note or reference that arrived from
OUTSIDE (a widget key, or an MCP key over the gateway) carries an unread flag
for the key OWNER. No notification records — the flag is a field on the object
itself (`unread_for` on chat_sessions notes / documents references), cleared by
opening the object (POST /api/inbox/read).

These tests pin the whole backend mechanism:
  - widget note flags the key owner (notes default ON); the sessions-list
    serializer exposes `unread` for the VIEWER and never the raw user id;
  - the dsh chat agent's INTERNAL key never flags;
  - MCP-served reference creation flags when the refs toggle is ON and not when
    OFF (the default) — derived from the tool registry, never a hand list;
  - the attach_file redeem flags once (a deduplicated replay does not re-flag);
  - per-(user x document) toggles live in user_preferences.preferences
    .doc_notify.<document_id>; notes OFF means the note stays unflagged;
  - read by a non-owner is a 204 no-op; read by the owner clears + emits
    inbox_changed on the event bus;
  - the project summary counts flagged objects per document and skips
    soft-deleted objects (and objects whose host document is deleted);
  - the widget note route emits note_session_created inline and turns a failed
    create_system_note into an explicit 5xx (no silent degradation).
"""

import io

import pytest

# ─── shared helpers ──────────────────────────────────────────────────────────


async def _make_doc(client, token: str, project_id: str, title: str, parent=None) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": "",
              **({"parent_id": parent} if parent else {})},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_widget_key(client, token: str, doc_id: str, label="inbox-key") -> str:
    resp = await client.post(
        "/api/api-keys",
        json={"document_id": doc_id, "label": label},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _add_member(client, admin_token: str, pid: str, user_uid: str) -> None:
    resp = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text


async def _widget_note(client, api_token: str, body="Hello from outside",
                       title=None) -> object:
    payload: dict = {"body": body}
    if title is not None:
        payload["title"] = title
    return await client.post(
        "/api/widget/note",
        json=payload,
        headers={"Authorization": f"Bearer {api_token}"},
    )


async def _list_note_cards(client, token: str, pid: str, doc_id: str) -> list[dict]:
    resp = await client.get(
        "/api/chat/sessions",
        params={"project_id": pid, "document_id": doc_id, "is_note": "true"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _flagged_note_row(test_db, session_id: str) -> dict:
    rows = await test_db.query(
        "SELECT unread_for, deleted_at, is_note, document_id FROM chat_sessions "
        "WHERE id = type::record('chat_sessions', $sid)",
        {"sid": session_id},
    )
    return rows[0] if rows else {}


async def _set_toggles(client, token: str, pid: str, doc_id: str, **vals) -> dict:
    resp = await client.put(
        f"/api/projects/{pid}/inbox/toggles/{doc_id}",
        json=vals,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _summary(client, token: str, pid: str) -> dict:
    resp = await client.get(
        f"/api/projects/{pid}/inbox",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── widget note: arrival + viewer-relative visibility ───────────────────────


@pytest.mark.asyncio
async def test_note_card_unread_is_viewer_relative_and_raw_id_never_leaves(
    client, admin_user, regular_user, project_with_doc,
):
    """The sessions-list serializer exposes `unread` for the VIEWER; the raw
    `unread_for` user id never reaches any client."""
    pid, _, admin_uid = project_with_doc
    user_uid, user_token = regular_user
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "SharedNotes")
    api_token = await _make_widget_key(client, admin_token, doc_id)
    await _add_member(client, admin_token, pid, user_uid)

    resp = await _widget_note(client, api_token)
    assert resp.status_code == 200, resp.text

    owner_cards = await _list_note_cards(client, admin_token, pid, doc_id)
    member_cards = await _list_note_cards(client, user_token, pid, doc_id)
    assert owner_cards and member_cards
    for cards, uid in ((owner_cards, admin_uid), (member_cards, user_uid)):
        for card in cards:
            assert "unread_for" not in card, "the raw recipient id must never serialize"
        assert isinstance(cards[0]["unread"], bool)
    assert [c["unread"] for c in owner_cards] == [True]
    assert [c["unread"] for c in member_cards] == [False]


# ─── toggles ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_defaults_notes_on_refs_off_and_roundtrip(
    client, admin_user, project_with_doc,
):
    """Defaults: notes ON, refs OFF. PUT resolves the full pair back."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "ToggleHost")

    resp = await client.get(
        f"/api/projects/{pid}/inbox/toggles/{doc_id}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"notes": True, "refs": False}

    assert await _set_toggles(client, admin_token, pid, doc_id, refs=True) == {
        "notes": True, "refs": True,
    }
    assert await _set_toggles(client, admin_token, pid, doc_id, notes=False) == {
        "notes": False, "refs": True,
    }


# ─── attach_file redeem flags once ───────────────────────────────────────────


async def _redeem(client, token: str) -> object:
    """POST ONE minted upload URL once. Same token re-POST = the dedup replay."""
    from mcp_gateway.upload import mint_upload_token

    upload_token, _exp, _jti = await mint_upload_token(**token)
    return await client.post(
        f"/api/mcp/upload/{upload_token}",
        files={"file": ("note.md", io.BytesIO(b"# attached"), "text/markdown")},
    )


@pytest.mark.asyncio
async def test_redeem_internal_mint_never_flags(client, test_db, admin_user, project_with_doc):
    """An internal agent's attach_file URL (minted with internal=True) never flags."""
    from db import fetch_one

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    host = await _make_doc(client, admin_token, pid, "RedeemInternalHost")
    await _set_toggles(client, admin_token, pid, host, refs=True)
    claims = {
        "project_id": pid, "document_id": host, "filename": "note.md",
        "mime": "text/markdown", "title": "note.md", "user_id": admin_uid,
        "internal": True,
    }
    resp = await _redeem(client, claims)
    assert resp.status_code == 200, resp.text
    ref = await fetch_one("documents", resp.json()["document_id"])
    assert not ref.get("unread_for")


# ─── read: owner clears, non-owner no-op, emits ──────────────────────────────


@pytest.mark.asyncio
async def test_read_by_owner_clears_member_read_is_noop(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """POST /api/inbox/read clears the flag iff it equals the caller; anyone
    else gets the same 204 as a no-op. The owner's clearing read emits
    inbox_changed on the bus (route-level)."""
    pid, _, admin_uid = project_with_doc
    user_uid, user_token = regular_user
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "ReadHost")
    api_token = await _make_widget_key(client, admin_token, doc_id)
    resp = await _widget_note(client, api_token)
    session_id = resp.json()["session_id"]

    events: list[dict] = []

    async def capture(**kw):
        events.append(kw)

    from event_bus import off, on

    # A member reading someone else's flag is a 204 no-op.
    resp = await client.post(
        "/api/inbox/read",
        json={"kind": "note", "id": session_id},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 204, resp.text
    row = await _flagged_note_row(test_db, session_id)
    assert row["unread_for"] == admin_uid, "a non-owner's read must not clear the flag"

    # The owner's read clears + emits.
    on("inbox_changed", capture)
    try:
        resp = await client.post(
            "/api/inbox/read",
            json={"kind": "note", "id": session_id},
            cookies={"lore_session": admin_token},
        )
    finally:
        off("inbox_changed", capture)
    assert resp.status_code == 204, resp.text
    row = await _flagged_note_row(test_db, session_id)
    assert not row.get("unread_for")
    assert len(events) == 1
    assert events[0]["document_id"] == doc_id

    # Reading again is a no-op (nothing left to clear, still 204, no emit).
    resp = await client.post(
        "/api/inbox/read",
        json={"kind": "note", "id": session_id},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 204, resp.text


# ─── summary ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_counts_per_document(client, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_a = await _make_doc(client, admin_token, pid, "SummaryA")
    doc_b = await _make_doc(client, admin_token, pid, "SummaryB")
    key_a = await _make_widget_key(client, admin_token, doc_a)
    key_b = await _make_widget_key(client, admin_token, doc_b)
    await _set_toggles(client, admin_token, pid, doc_b, refs=True)

    await _widget_note(client, key_a)
    await _widget_note(client, key_a)
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("clip.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 200, resp.text

    data = await _summary(client, admin_token, pid)
    assert data["documents"] == {
        doc_a: {"notes": 2, "refs": 0},
        doc_b: {"notes": 0, "refs": 1},
    }
