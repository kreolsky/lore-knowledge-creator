"""Tests for the import_file agent/MCP tool (plan tool-surface-consolidation Step 2b).

ONE tool (import_file) replaces the former upload_document + upload_reference,
discriminated by `node_type`. text/markdown/.docx via the import pipeline
(markdown normalize for .md, image extraction); binary image/audio via the widget's
save path (thumbnail/transcription). Binary requires node_type="reference".

D2: plain `content` (authored text) is NOT an input — authored text goes to
create_document (which may propose). The .md normalize capability moved to
create_document's `normalize` flag (D3); those tests live here too. Auto-only on
BOTH surfaces (no ProposalKind) — the Pi driver sees status:"applied" and shows no
proposal card.

These pin the import-pipeline funnel (is_text_bytes gate, conditional
normalize_markdown, sync docx converter, extract_and_replace_images), the
record-factory branch (create_document_via_collab / create_reference_via_collab),
the binary format dispatch, the dual-surface invariant, and the access gates.
"""

import base64
import hashlib
import secrets
from unittest.mock import AsyncMock, patch

import pytest

# Minimal valid DOCX magic: a ZIP local-file-header signature.
DOCX_MAGIC = b"PK\x03\x04" + b"\x00" * 64

# 1x1 PNG bytes (magic-valid) — for the binary + image-extraction paths.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ─── Shared helpers (mirror test_append_move_tools / test_mcp_gateway_mutating) ─


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"up-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token, project_id, title, content="", parent_id=None):
    body = {"project_id": project_id, "title": title, "content": content}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _read_content(client, token, doc_id):
    resp = await client.get(f"/api/documents/{doc_id}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json().get("content") or ""


async def _read_doc(client, token, doc_id):
    """Read a document/reference row directly (references are NOT in the project
    tree — /api/projects/{id} filters is_reference=false)."""
    resp = await client.get(f"/api/documents/{doc_id}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── sandbox_path test seams (D5: content_base64 left the Pi surface, so the
# binary pipeline is exercised through sandbox_path — the byte channel the agent
# actually has. Mirrors test_sandbox_file_bridge's sftp_seams pattern.) ──────


async def _make_internal_key(test_db, user_id, project_id):
    """An INTERNAL whole-project key — the kind the Pi driver mints for itself and
    the only kind the sandbox_path console gate accepts."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"up-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": True, "internal": True,
    })
    return token


@pytest.fixture
def sandbox_configured(monkeypatch):
    """Pretend a sandbox is configured (config.SANDBOX_ENABLED is imported BY VALUE
    at module import, so patch the consumer's own module attribute)."""
    from helpers import pin_tool_gates
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    pin_tool_gates(monkeypatch, sandbox=True)


@pytest.fixture
def sftp_seams(monkeypatch, sandbox_configured):
    """Replace the SFTP read seam with a mutable data knob so a test can feed
    arbitrary bytes (PNG / DOCX / MP3) as if read from the workspace."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    state = {"realpath": None, "data": PNG_BYTES}

    async def _fake_read_checked(ws_path, max_bytes):
        return state["realpath"] or ws_path, state["data"]

    monkeypatch.setattr(sandbox.files, "_sftp_read_checked", _fake_read_checked)
    return state


# ════════════════════════════════════════════════════════════════════════════
# create_document — normalize flag (D3: the .md normalize capability moved here
# from the removed upload `content` path)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_document_normalize_true_reflows_md_text(
    client, test_db, admin_user, project_with_doc,
):
    """D3: normalize=True applies normalize_markdown — hard-wrapped prose is
    reflowed (the capability that left the upload surface with `content` in D2)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

    wrapped = (
        "This is a paragraph that has been hard wrapped at a fixed column\n"
        "and continues onto the following line and keeps going further\n"
        "and yet another wrapped line of prose to trip the reflow heuristic\n"
    )
    resp = await client.post("/api/tool/create_document", json={
        "title": "Imported", "content": wrapped, "normalize": True,
        "parent_id": None, "apply": "auto",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["doc_id"]
    content = await _read_content(client, token, doc_id)
    # Reflowed: the soft-wrapped newlines are folded into one logical line.
    assert "\n" not in content.strip()


# ════════════════════════════════════════════════════════════════════════════
# import_file — docx (sandbox_path + ZIP magic → sync converter) → document or
# reference. D5: content_base64 left the Pi surface; the binary pipeline is fed
# through sandbox_path (the byte channel the agent actually has).
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_docx_converter_failure_is_502(
    client, test_db, admin_user, project_with_doc, sftp_seams,
):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = DOCX_MAGIC

    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock, side_effect=RuntimeError("pandoc died")):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "bad.docx", "sandbox_path": "outputs/bad.docx",
        }, headers=_hdr(agent_tok))
    assert resp.status_code == 502, resp.text


# ════════════════════════════════════════════════════════════════════════════
# import_file — binary image/audio → reference (node_type="reference")
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# import_file — .zip archive (sandbox_path) → media_type "file" reference
# (agent zip sharing; plan agent-zip-reference). Extension-first dispatch: a
# `.zip` NEVER reaches the docx converter even though both share the PK\x03\x04
# magic. The archive is never opened — only its magic is validated.
# ════════════════════════════════════════════════════════════════════════════


# Minimal zip payload: ZIP local-file-header magic + padding (magic-valid).
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 128


@pytest.mark.asyncio
async def test_import_file_zip_creates_file_reference(
    client, test_db, admin_user, project_with_doc, sftp_seams, enqueue_recorder,
):
    """A workspace .zip (sandbox_path) → a `media_type: "file"` reference: stored
    bytes served verbatim by the generic files route, mime application/zip, no
    processing_status, and NOTHING derived (no thumbnail, no transcription)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "ZipHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)
    sftp_seams["data"] = ZIP_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "page.zip", "node_type": "reference", "parent_id": host,
        "sandbox_path": "outputs/page.zip",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["is_reference"] is True
    assert ref["parent_id"] == host
    assert ref["media_type"] == "file"
    # None (or key-absent — the API drops NONE fields): a file ref derives nothing.
    assert not ref.get("processing_status")
    meta = ref.get("file_meta") or {}
    assert meta.get("mime_type") == "application/zip"
    assert meta.get("original_name") == "page.zip"
    assert meta.get("file_size") == len(ZIP_BYTES)

    # The generic authed serve route returns the stored bytes verbatim.
    serve = await client.get(
        f"/api/files/{data['reference_id']}/{meta['original_name']}",
        cookies={"lore_session": token},
    )
    assert serve.status_code == 200
    assert serve.content == ZIP_BYTES
    assert serve.headers["content-type"].startswith("application/zip")

    # A file reference derives nothing — no thumbnail_task, no other job.
    assert [n for n, _a, _k in enqueue_recorder.calls if n == "thumbnail_task"] == []


# ════════════════════════════════════════════════════════════════════════════
# binary REJECTED for a document (default node_type) — names node_type
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# message_id + attachment_index — the backend resolves ONE addressed chat image
# (any message of the calling conversation, or any message the chat-recall wall
# admits). Plan chat-image-import.
# ════════════════════════════════════════════════════════════════════════════


_PNG_URI = f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode()}"


async def _seed_user_message_with_images(
    test_db, session_id, msg_id, images, *, user_id, project_id, content="add this",
    **session_fields,
):
    """Insert an OWNED chat session row plus a user message row carrying `images`
    (data-URI list), mirroring how a chat turn persists attachments (chat_id =
    session_id, role='user'). The session row is required: attachment resolution
    validates X-Agent-Session-Id against chat_sessions before reading messages."""
    from db import create_record

    await create_record("chat_sessions", session_id, {
        "user_id": user_id, "project_id": project_id, "title": "",
        **session_fields,
    })
    await _seed_message(test_db, msg_id, session_id, content, images)


async def _seed_message(test_db, msg_id, chat_id, content, images):
    """One user message row in an EXISTING session (the session row is not
    re-created — the world seed already owns it)."""
    from db import create_record

    await test_db.query("DELETE type::record('messages', $id)", {"id": msg_id})
    await create_record("messages", msg_id, {
        "chat_id": chat_id, "role": "user", "content": content, "images": images,
    })


async def _seed_attachment_world(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """The admission world: the caller's CURRENT session (an older image
    message + a newer plain one), an OTHER chat with an image, and another
    user's chat — plus the keys to ask from each side of every wall."""
    from db import create_record

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    other_uid, _ = regular_user
    await create_record("project_members", "test-pm-att-world", {
        "project_id": pid, "user_id": other_uid, "access_level": "full",
    })
    world = {
        "pid": pid,
        "host": await _make_doc(client, token, pid, "AttWorldHost"),
        "ka": await _make_internal_key(test_db, admin_uid, pid),
        "kext": await _make_agent_key(test_db, admin_uid, pid),
    }
    # the calling conversation: an image-bearing OLDER turn, then a plain turn
    await _seed_user_message_with_images(
        test_db, "sCur", "mOld", [_PNG_URI, _PNG_URI],
        user_id=admin_uid, project_id=pid,
    )
    await test_db.query(
        "CREATE type::record('messages', 'mNew') CONTENT "
        "{chat_id: 'sCur', role: 'user', content: 'plain follow-up', "
        "parent_id: 'mOld'}")
    # an OTHER chat of the same user (visible to recall)
    await _seed_user_message_with_images(
        test_db, "sOther", "mOther", [_PNG_URI],
        user_id=admin_uid, project_id=pid,
    )
    # another user's chat
    await _seed_user_message_with_images(
        test_db, "sB", "mB", [_PNG_URI],
        user_id=other_uid, project_id=pid,
    )
    return world


async def _import_attached(client, token, message_id, index=0, session_id="sCur",
                           status=200, host=None, omit_message_id=False):
    body = {
        "filename": "pic.png", "node_type": "reference",
        "parent_id": host, "attachment_index": index,
    }
    if not omit_message_id:
        body["message_id"] = message_id
    resp = await client.post("/api/tool/import_file", json=body, headers={
        "Authorization": f"Bearer {token}", "X-Agent-Session-Id": session_id,
    })
    assert resp.status_code == status, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_attachment_index_refuses_a_foreign_session(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    """attachment_index resolves images via X-Agent-Session-Id: a session id
    the caller does not OWN (foreign user's real session, or no session row at
    all) must be refused before any message is read — the admission keys the
    own-conversation arm on the raw header value, so without this check a
    caller tagging another user's session id would silently import THAT user's
    attached image (cross-user image read). Refused with the SAME 400 the
    absent header gets: "not a resolvable chat session of yours" is one
    condition with several spellings, and none of them may leak what the other
    session holds."""
    from db import create_record

    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    other_uid, _ = regular_user
    await create_record("project_members", "test-pm-att-foreign", {
        "project_id": pid, "user_id": other_uid, "access_level": "full",
    })
    host = await _make_doc(client, token, pid, "AttForeignHost")
    other_tok = await _make_internal_key(test_db, other_uid, pid)

    session_id = "test-att-session-foreign"
    await create_record("chat_sessions", session_id, {
        "user_id": admin_uid, "project_id": pid, "title": "",
    })
    await create_record("messages", "test-att-msg-foreign", {
        "chat_id": session_id, "role": "user", "content": "x", "images": [_PNG_URI],
    })

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "node_type": "reference", "parent_id": host,
        "message_id": "test-att-msg-foreign", "attachment_index": 0,
    }, headers={**_hdr(other_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 400, resp.text
    assert "chat session" in resp.json()["detail"]


# TEST-WHY: refused-principal — every wall the chat-image admission owes: the recall wall (hidden subtree, unindexed, another user, dead message/branch), the off-switch (403 for other chats, never for the calling conversation), the in-chat gates (external key, foreign header), the link refusal, and the 422 that names the fix for the removed latest-message default.
@pytest.mark.parametrize(
    ("case", "status", "detail"),
    [
        pytest.param("other-chat", 200, None, id="other-visible-chat-created"),
        pytest.param("hidden-subtree", 404, "message not found",
                     id="viewer-outside-chats-unindexed-subtree"),
        pytest.param("other-user", 404, "message not found",
                     id="another-users-message"),
        pytest.param("deleted-message", 404, "message not found",
                     id="deleted-message"),
        pytest.param("unindexed-chat", 404, "message not found",
                     id="unindexed-chat"),
        pytest.param("deleted-branch", 404, "message not found",
                     id="deleted-branch-of-own-conversation"),
        pytest.param("recall-off-other", 403, "chat-recall is disabled",
                     id="recall-disabled-other-chat"),
        pytest.param("recall-off-own", 200, None,
                     id="recall-disabled-own-conversation-still-created"),
        pytest.param("https-link", 400, "image is a link, not stored in Lore",
                     id="https-image-is-never-fetched"),
        pytest.param("index-out-of-range", 400, "out of range",
                     id="index-out-of-range"),
        pytest.param("missing-message-id", 422, "requires `message_id`",
                     id="attachment-index-without-message-id"),
        pytest.param("external-key", 400, "only available in a chat session",
                     id="non-internal-key"),
    ],
)
async def test_attachment_admission(client, test_db, admin_user, regular_user, project_with_doc, case, status, detail):
    """The admission contract of `import_file(message_id, attachment_index)`,
    one parametrized arm each: the calling conversation is admitted wholesale
    (off-switch included — today's attachment path never consulted it either),
    every other message runs the chat-recall wall verbatim; a linked image is
    never fetched; the bare `attachment_index` (removed latest-message
    default) is a 422 naming the fix."""
    w = await _seed_attachment_world(
        client, test_db, admin_user, regular_user, project_with_doc)
    admin_uid, admin_tok = admin_user
    message_id, index, key = "mOther", 0, w["ka"]
    if case == "hidden-subtree":
        f = await _make_doc(client, admin_tok, w["pid"], "AttFlagged")
        c = await _make_doc(client, admin_tok, w["pid"], "AttHidden", parent_id=f)
        await test_db.query(
            "UPDATE type::record('documents', $id) SET chats_unindexed = true",
            {"id": f})
        await _seed_user_message_with_images(
            test_db, "sHid", "mHid", [_PNG_URI],
            user_id=admin_uid, project_id=w["pid"], content="hidden",
            document_id=c,
        )
        message_id = "mHid"
    elif case == "other-user":
        message_id = "mB"
    elif case == "deleted-message":
        await test_db.query(
            "UPDATE type::record('messages', 'mOther') SET deleted_at = time::now()")
    elif case == "unindexed-chat":
        await test_db.query(
            "UPDATE type::record('chat_sessions', 'sOther') SET unindexed = true")
    elif case == "deleted-branch":
        # a branch of the calling conversation, deleted like delete_session
        # does: messages first, then the session row
        await _seed_user_message_with_images(
            test_db, "sBranch", "mBranch", [_PNG_URI],
            user_id=admin_uid, project_id=w["pid"], content="branch",
            thread_id="sCur",
        )
        await test_db.query(
            "UPDATE messages SET deleted_at = time::now() "
            "WHERE chat_id = 'sBranch' AND deleted_at IS NONE")
        await test_db.query(
            "UPDATE type::record('chat_sessions', 'sBranch') "
            "SET deleted_at = time::now()")
        message_id = "mBranch"
    elif case in ("recall-off-other", "recall-off-own"):
        await test_db.query(
            "CREATE instance_skills CONTENT {name: 'chat-recall', enabled: false, "
            "updated_by: 'admin'}")
        if case == "recall-off-own":
            message_id = "mOld"
    elif case == "https-link":
        await _seed_message(test_db, "mLink", "sCur", "a link",
                            ["https://example.com/pic.png"])
        message_id = "mLink"
    elif case == "index-out-of-range":
        message_id, index = "mOld", 5
    elif case == "missing-message-id":
        result = await _import_attached(
            client, key, "mOther", status=422, host=w["host"],
            omit_message_id=True)
        assert "message_id" in str(result["detail"])
        return
    elif case == "external-key":
        key = w["kext"]
        message_id = "mOld"
    try:
        result = await _import_attached(
            client, key, message_id, index=index, status=status, host=w["host"])
    finally:
        if case in ("recall-off-other", "recall-off-own"):
            await test_db.query(
                "DELETE instance_skills WHERE name = 'chat-recall'")
    if status == 200:
        assert result["status"] == "applied"
        ref = await _read_doc(client, admin_tok, result["reference_id"])
        assert ref["media_type"] == "image"
        assert (ref.get("file_meta") or {}).get("mime_type") == "image/png"
    else:
        # the refused arms are exactly the in-chat 400s, the off-switch 403,
        # the uniform 404s and the contract 422 — never a 5xx, never a leak
        assert status in (400, 403, 404, 422)
        assert detail in str(result["detail"])


# ════════════════════════════════════════════════════════════════════════════
# D1 (plan internal-agent-surface-reconciliation): the BYTES decide the type on
# the attachment path — the model cannot see the bytes, so its filename guess
# may never override the mime the backend already holds (the data-URI header).
# The model's filename contributes its STEM only.
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_import_file_attachment_index_webp_creates_reference(
    client, test_db, admin_user, project_with_doc,
):
    """The addressing contract + D1+D2: an OLDER message of the calling
    conversation (a plain-text turn follows it — the removed latest-message
    default would have found nothing) is imported by id; a webp attachment
    (the format the frontend emits for chat images) resolves to an image
    reference. Before D2 the .webp extension died as 'Unsupported binary
    type'; before D1 the model's filename guess overrode the known mime →
    magic mismatch → 400."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "WebpHost")
    agent_tok = await _make_internal_key(test_db, admin_uid, pid)

    # Minimal RIFF….WEBP — validate_magic keys off the RIFF magic + image/webp.
    webp = b"RIFF\x1a\x00\x00\x00WEBP" + b"\x00" * 32
    session_id = "test-att-webp"
    await _seed_user_message_with_images(
        test_db, session_id, "test-att-msg-old",
        [_PNG_URI, f"data:image/webp;base64,{base64.b64encode(webp).decode()}"],
        user_id=admin_uid, project_id=pid, content="two pictures",
    )
    await test_db.query(
        "CREATE type::record('messages', 'test-att-msg-new') CONTENT "
        "{chat_id: $sid, role: 'user', content: 'plain follow-up', "
        "parent_id: 'test-att-msg-old'}",
        {"sid": session_id})

    resp = await client.post("/api/tool/import_file", json={
        "filename": "shot.webp", "node_type": "reference", "parent_id": host,
        "message_id": "test-att-msg-old", "attachment_index": 1,
    }, headers={**_hdr(agent_tok), "X-Agent-Session-Id": session_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    ref = await _read_doc(client, token, data["reference_id"])
    assert ref["media_type"] == "image"
    assert (ref.get("file_meta") or {}).get("mime_type") == "image/webp"
