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

import hashlib
import io
import secrets
from unittest.mock import AsyncMock, patch

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


def _mcp_ctx(uid: str, name: str, pid: str, *, internal=False) -> dict:
    """A handcrafted MCP key context, the shape resolve_api_key returns."""
    return {
        "user_id": uid, "project_id": pid, "scope_root": "",
        "key_id": "test-key", "key_label": None,
        "capabilities": ["agent"], "auto_apply": True, "internal": internal,
        "user": {"user_id": uid, "name": name, "role": "user"},
        "call_id": None, "session_id": None,
    }


# ─── widget note: arrival + viewer-relative visibility ───────────────────────


@pytest.mark.asyncio
async def test_widget_note_flags_key_owner(client, test_db, admin_user, project_with_doc):
    """POST /api/widget/note creates a system note and flags it for the key owner."""
    from db import fetch_one

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "NoteHost")
    api_token = await _make_widget_key(client, admin_token, doc_id)

    resp = await _widget_note(client, api_token)
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    assert session_id

    note = await fetch_one("chat_sessions", session_id)
    assert note["is_note"] is True
    assert note["document_id"] == doc_id
    assert note["unread_for"] == admin_uid


@pytest.mark.asyncio
async def test_widget_note_title_uses_key_label(client, admin_user, project_with_doc):
    """The note title is the key label alone, or `<label> — <title>` when given."""
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "TitleHost")
    api_token = await _make_widget_key(client, admin_token, doc_id, label="field-recorder")

    resp = await _widget_note(client, api_token)
    assert resp.status_code == 200, resp.text
    plain = await fetch_one("chat_sessions", resp.json()["session_id"])
    assert plain["title"] == "field-recorder"

    resp = await _widget_note(client, api_token, title="Evening notes")
    assert resp.status_code == 200, resp.text
    titled = await fetch_one("chat_sessions", resp.json()["session_id"])
    assert titled["title"] == "field-recorder — Evening notes"


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


@pytest.mark.asyncio
async def test_internal_key_note_never_flags(client, test_db, admin_user, project_with_doc):
    """The dsh chat agent's internal key creates the note but never flags it."""
    from db import create_record, fetch_one

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "InternalHost")
    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"internal-{secrets.token_hex(4)}", {
        "user_id": admin_uid, "project_id": pid, "document_id": doc_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "driver", "capabilities": ["widget"], "internal": True,
    })

    resp = await _widget_note(client, token)
    assert resp.status_code == 200, resp.text
    note = await fetch_one("chat_sessions", resp.json()["session_id"])
    assert note["is_note"] is True
    assert not note.get("unread_for"), "an internal key must never flag"


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


@pytest.mark.asyncio
async def test_notes_off_means_widget_note_stays_unflagged(
    client, test_db, admin_user, project_with_doc,
):
    """notes OFF ⇒ the object is created unflagged (missed = lost)."""
    from db import fetch_one

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "MutedHost")
    api_token = await _make_widget_key(client, admin_token, doc_id)
    await _set_toggles(client, admin_token, pid, doc_id, notes=False)

    resp = await _widget_note(client, api_token)
    assert resp.status_code == 200, resp.text
    note = await fetch_one("chat_sessions", resp.json()["session_id"])
    assert not note.get("unread_for")


# ─── MCP: reference creation flags (registry-derived) ────────────────────────


def _mcp_served_reference_factory_tools() -> set[str]:
    """Derive the MCP-served tools whose handler (or the tool_api sibling module
    it delegates its apply-primitives to — one import hop) touches a reference-
    creation factory (create_reference_via_collab / create_reference_row /
    _create_binary_reference). AST over source — a new MCP tool that starts
    creating references enters this set automatically, and the test below
    demands the dispatch hook covers it."""
    import ast
    import pathlib

    from agent_tools import registry

    factories = {"create_reference_via_collab", "create_reference_row",
                 "_create_binary_reference"}
    app_root = pathlib.Path("/app")

    def _imports_of(tree: ast.AST) -> set[str]:
        """Absolute `from X import ...` module names (the one-hop follow set)."""
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                out.add(node.module)
        return out

    def _touches_factories(module: str) -> bool:
        path = app_root / (module.replace(".", "/") + ".py")
        if not path.exists():
            return False
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and node.id in factories) or (
                isinstance(node, ast.Attribute) and node.attr in factories
            ):
                return True
        return False

    out: set[str] = set()
    for name, entry in registry.REGISTRY.items():
        if "mcp" not in entry.surfaces:
            continue
        handler_module = entry.handler_path.split(":")[0]
        if _touches_factories(handler_module):
            out.add(name)
            continue
        # One hop: the handler may delegate the write to a sibling tool_api
        # module (create_document → _common's apply primitive).
        tree = ast.parse((app_root / (handler_module.replace(".", "/") + ".py")).read_text())
        if any(_touches_factories(m) for m in _imports_of(tree)):
            out.add(name)
    return out


@pytest.mark.asyncio
async def test_registry_derives_create_document_and_dispatch_flags_it(
    client, test_db, admin_user, project_with_doc, mcp_running,
):
    """The registry walk derives create_document as a reference-creating MCP
    tool; dispatch_tool with a non-internal ctx flags the new reference for the
    key owner when the refs toggle is ON, and leaves it unflagged when OFF (the
    default)."""
    from mcp_gateway.dispatch import dispatch_tool

    from db import fetch_one

    names = _mcp_served_reference_factory_tools()
    assert names, "registry derivation broke: no MCP tool touches a reference factory"
    assert "create_document" in names

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    host = await _make_doc(client, admin_token, pid, "McpHost")
    ctx = _mcp_ctx(admin_uid, "testadmin", pid)

    # Default: refs OFF → no flag.
    res = await dispatch_tool("create_document", {
        "node_type": "reference", "parent_id": host,
        "title": "From gateway", "content": "body",
    }, dict(ctx))
    ref = await fetch_one("documents", res["reference_id"])
    assert ref["is_reference"] is True
    assert not ref.get("unread_for"), "refs default OFF — the reference must not flag"

    # Toggle ON → the next MCP-created reference flags.
    await _set_toggles(client, admin_token, pid, host, refs=True)
    res = await dispatch_tool("create_document", {
        "node_type": "reference", "parent_id": host,
        "title": "From gateway 2", "content": "body",
    }, dict(ctx))
    ref = await fetch_one("documents", res["reference_id"])
    assert ref.get("unread_for") == admin_uid


@pytest.mark.asyncio
async def test_mcp_internal_ctx_never_flags(
    client, test_db, admin_user, project_with_doc, mcp_running,
):
    """The dsh driver's internal key creates references that never flag."""
    from mcp_gateway.dispatch import dispatch_tool

    from db import fetch_one

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    host = await _make_doc(client, admin_token, pid, "McpInternalHost")
    await _set_toggles(client, admin_token, pid, host, refs=True)
    ctx = _mcp_ctx(admin_uid, "testadmin", pid, internal=True)

    res = await dispatch_tool("create_document", {
        "node_type": "reference", "parent_id": host,
        "title": "Internal", "content": "body",
    }, ctx)
    ref = await fetch_one("documents", res["reference_id"])
    assert not ref.get("unread_for")


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
async def test_redeem_flags_owner_once_refs_on_and_not_when_off(
    client, test_db, admin_user, project_with_doc,
):
    """The redeem route flags the minting user for a reference when refs are ON,
    not when OFF (default), and a deduplicated replay of the SAME url does not
    re-flag (the flag was already cleared — a replay must not resurrect it)."""
    from mcp_gateway.upload import mint_upload_token

    from db import fetch_one

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    host = await _make_doc(client, admin_token, pid, "RedeemHost")
    claims = {
        "project_id": pid, "document_id": host, "filename": "note.md",
        "mime": "text/markdown", "title": "note.md", "user_id": admin_uid,
    }

    # Default: refs OFF → created unflagged.
    resp = await _redeem(client, claims)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deduplicated"] is False
    ref = await fetch_one("documents", body["document_id"])
    assert not ref.get("unread_for")

    # Toggle ON → flag; replay of the SAME url does not re-flag.
    await _set_toggles(client, admin_token, pid, host, refs=True)
    token, _exp, _jti = await mint_upload_token(**claims)
    resp = await client.post(
        f"/api/mcp/upload/{token}",
        files={"file": ("note.md", io.BytesIO(b"# attached"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["deduplicated"] is False
    ref = await fetch_one("documents", first["document_id"])
    assert ref.get("unread_for") == admin_uid

    await inbox_mark_read_ref(client, admin_token, first["document_id"])

    resp = await client.post(
        f"/api/mcp/upload/{token}",
        files={"file": ("note.md", io.BytesIO(b"# attached"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deduplicated"] is True
    assert resp.json()["document_id"] == first["document_id"]
    ref = await fetch_one("documents", first["document_id"])
    assert not ref.get("unread_for"), "a dedup replay must not resurrect the flag"


async def inbox_mark_read_ref(client, token: str, ref_id: str) -> None:
    resp = await client.post(
        "/api/inbox/read",
        json={"kind": "ref", "id": ref_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 204, resp.text


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


@pytest.mark.asyncio
async def test_inbox_changed_payload_fields(client, admin_user, project_with_doc):
    """mark_read's inbox_changed carries project_id, user_id, document_id — the
    field names the project-ws handler and event-types.ts bind. emit is
    fire-and-forget (spawned tasks), so the test yields the loop before
    asserting."""
    import asyncio

    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "EmitHost")
    api_token = await _make_widget_key(client, admin_token, doc_id)
    resp = await _widget_note(client, api_token)
    session_id = resp.json()["session_id"]

    import inbox as inbox_module

    events: list[dict] = []

    async def capture(**kw):
        events.append(kw)

    from event_bus import off, on

    on("inbox_changed", capture)
    try:
        cleared = await inbox_module.mark_read("note", session_id, admin_uid)
        assert cleared is True
        for _ in range(4):
            await asyncio.sleep(0)  # let the bus's spawned subscriber tasks run
    finally:
        off("inbox_changed", capture)
    assert len(events) == 1
    assert events[0]["project_id"] == pid
    assert events[0]["user_id"] == admin_uid
    assert events[0]["document_id"] == doc_id


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


@pytest.mark.asyncio
async def test_summary_drops_soft_deleted_object_and_deleted_host(
    client, test_db, admin_user, project_with_doc,
):
    """A soft-deleted flagged object — and a flagged object whose HOST document
    was soft-deleted — drops out of the pool for everyone alike. Deleting does
    not clear the flag (one writer per field); the filter does the hiding."""

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_a = await _make_doc(client, admin_token, pid, "DelA")
    host = await _make_doc(client, admin_token, pid, "DelHost")
    key_a = await _make_widget_key(client, admin_token, doc_a)
    key_host = await _make_widget_key(client, admin_token, host)
    await _set_toggles(client, admin_token, pid, host, refs=True)

    resp = await _widget_note(client, key_a)
    note_id = resp.json()["session_id"]
    # Two flagged references UNDER the host (refs ON for host): the widget
    # upload flags through the route, so both flagged refs share one writer —
    # the per-object legs are what this test moves, not the writers.
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("clip.webm", io.BytesIO(b"\x1aE\xdf\xa3" + b"\x00" * 96), "audio/webm")},
        headers={"Authorization": f"Bearer {key_host}"},
    )
    ref_id = resp.json()["reference_id"]
    resp = await client.post(
        "/api/widget/upload",
        files={"file": ("second.md", io.BytesIO(b"body"), "text/markdown")},
        headers={"Authorization": f"Bearer {key_host}"},
    )
    assert resp.json()["reference_id"]  # the second flagged ref — never deleted

    data = await _summary(client, admin_token, pid)
    assert data["documents"][doc_a]["notes"] == 1
    assert data["documents"][host]["refs"] == 2

    # Soft-delete the flagged note → it drops out.
    await test_db.query(
        "UPDATE type::record('chat_sessions', $sid) SET deleted_at = time::now()",
        {"sid": note_id},
    )
    data = await _summary(client, admin_token, pid)
    assert doc_a not in data["documents"]

    # Soft-delete ONE flagged ref (the audio) → the count drops, the other stays.
    await test_db.query(
        "UPDATE type::record('documents', $rid) SET deleted_at = time::now()",
        {"rid": ref_id},
    )
    data = await _summary(client, admin_token, pid)
    assert data["documents"][host]["refs"] == 1

    # Soft-delete the HOST → its remaining flagged ref drops with it.
    await test_db.query(
        "UPDATE type::record('documents', $hid) SET deleted_at = time::now()",
        {"hid": host},
    )
    data = await _summary(client, admin_token, pid)
    assert host not in data["documents"]


# ─── widget note: realtime frame + explicit failure ──────────────────────────


@pytest.mark.asyncio
async def test_widget_note_emits_note_session_created(client, admin_user, project_with_doc):
    """The route emits note_session_created with the minimal {session_id} frame —
    the bus subscriber fans it to the doc channel and an open NotesPanel refreshes."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "RealtimeHost")
    api_token = await _make_widget_key(client, admin_token, doc_id)

    events: list[dict] = []

    async def capture(**kw):
        events.append(kw)

    from event_bus import off, on

    on("note_session_created", capture)
    try:
        resp = await _widget_note(client, api_token)
        assert resp.status_code == 200, resp.text
    finally:
        off("note_session_created", capture)

    assert len(events) == 1
    ev = events[0]
    assert ev["entity_type"] == "doc"
    assert ev["entity_id"] == doc_id
    assert ev["event"] == {"type": "note_session_created",
                           "session_id": resp.json()["session_id"]}


@pytest.mark.asyncio
async def test_widget_note_failed_creation_is_explicit_5xx(client, admin_user, project_with_doc):
    """create_system_note never raises and returns None on failure — the route
    turns None into an explicit 5xx (no silent degradation), and flags nothing."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = await _make_doc(client, admin_token, pid, "FailHost")
    api_token = await _make_widget_key(client, admin_token, doc_id)

    with patch("routes.widget.create_system_note",
               new_callable=AsyncMock, return_value=None):
        resp = await _widget_note(client, api_token)
    assert 500 <= resp.status_code < 600, resp.text
