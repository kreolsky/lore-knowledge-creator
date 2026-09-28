"""Binding tests for the review-fixes plan
(.kilo/plans/mcp-tool-surface-redesign-review-fixes.md) — findings D-A/D-B/D-C/D-D.

Every assertion binds a fix to the SAME source the implementation reads (the
classifier's advertised cap, the stored node's row, the real convergence core),
not a hand-copied literal, so the test and the code cannot drift.
"""

import asyncio
import os
import secrets
import struct

import pytest
import routes.chat.sessions  # noqa: F401 — ensure attr exists before patch()
from helpers import pin_stt_url

# ─── helpers (mirror test_mcp_tool_surface_redesign.py) ───────────────────────


def _wav_bytes(payload_size: int) -> bytes:
    """A minimal RIFF/WAVE file of roughly `payload_size` bytes of audio data."""
    data = b"\x00" * payload_size
    fmt = struct.pack("<4sIHHIIHH4sI", b"fmt ", 16, 1, 1, 8000, 8000, 1, 8, b"data", len(data))
    body = b"WAVE" + fmt + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=True, scope_root=""):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = __import__("hashlib").sha256(token.encode()).hexdigest()
    key_id = f"rf-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": scope_root,
        "token_hash": token_hash, "label": "reviewfix", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _result_text(resp_json):
    import json

    return json.loads(resp_json["result"]["content"][0]["text"])


def _is_error(resp_json):
    return resp_json.get("result", {}).get("isError", False)


async def _call(client, token, name, arguments=None):
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments or {}}},
        headers=_hdr(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _mint(client, token, **arguments):
    """Mint an attach_file url and return its decoded payload."""
    return _result_text(await _call(client, token, "attach_file", arguments))


def _patch_small_caps(monkeypatch, mb=1):
    """Drive every per-kind cap down to `mb` on config (the settings fallback
    leg — classify_upload_kind reads the caps through settings.get), so the
    mint's advertised cap AND the redeem's enforcement BOTH use the same small
    number (and the over-cap payload stays tiny)."""
    import config

    for name in ("MAX_AUDIO_SIZE_MB", "MAX_IMAGE_SIZE_MB",
                 "MAX_MARKDOWN_SIZE_MB", "MAX_DOCX_SIZE_MB"):
        monkeypatch.setattr(config, name, mb, raising=True)


def _over_payload(kind: str, size: int) -> bytes:
    """A payload of `size` bytes whose magic matches `kind` (so a missing cap
    reaches save_upload, isolating the cap as the only thing that can reject)."""
    if kind == "audio":
        return _wav_bytes(size)
    if kind == "docx":
        return b"PK\x03\x04" + b"\x00" * (size - 4)
    if kind == "image":
        return b"\x89PNG" + b"\x00" * (size - 4)
    # markdown: no magic gate; plain text over the cap.
    return b"# title\n" + b"x" * (size - 8)


_KIND_CASES = [
    ("docx", "doc.docx"),
    ("markdown", "note.md"),
    ("image", "shot.png"),
    ("audio", "clip.wav"),
]


def _mime_for(kind: str) -> str:
    from files_util import DOCX_MIME

    return {
        "docx": DOCX_MIME, "markdown": "text/markdown",
        "image": "image/png", "audio": "audio/wav",
    }[kind]


def _project_listing(pid) -> set:
    from config import STORAGE_PATH

    d = STORAGE_PATH / pid
    if not d.is_dir():
        return set()
    return set(os.listdir(d))


# ═══ D-A — the redeem enforces the cap it advertises, for every kind ═══════════


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,filename", _KIND_CASES)
async def test_attach_over_cap_is_413_and_creates_nothing(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
    kind, filename,
):
    """Mint → POST bytes OVER the advertised cap → 413, with NO node created and
    NO reference directory orphaned under storage. The cap is read from
    classify_upload_kind (the SAME number the mint advertised), not a literal, so
    a future fifth kind is covered by this same test."""
    from files_util import classify_upload_kind

    pid, doc_id, admin_uid = project_with_doc
    pin_stt_url(monkeypatch, "")
    _patch_small_caps(monkeypatch, mb=1)
    key = await _make_agent_key(test_db, admin_uid, pid)

    mime = _mime_for(kind)
    _kind, cap_mb = await classify_upload_kind(filename, mime)
    over = cap_mb * 1024 * 1024 + 4096  # cap + 4 KB → strictly over

    mint = await _mint(client, key, attach_to=doc_id, filename=filename)
    assert mint["max_size_mb"] == cap_mb  # binds the advertised number

    before = _project_listing(pid)
    resp = await client.post(
        mint["url"], files={"file": (filename, _over_payload(kind, over), mime)},
    )
    assert resp.status_code == 413, resp.text
    # No reference node was created under the host.
    from db import get_db

    rows = await (await get_db()).query(
        "SELECT id FROM documents WHERE parent_id = $h AND is_reference = true "
        "AND deleted_at IS NONE",
        {"h": doc_id},
    )
    assert rows == [], rows
    # No orphaned reference directory appeared under storage.
    assert _project_listing(pid) == before, "an over-cap POST left a directory on disk"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,filename", [c for c in _KIND_CASES if c[0] != "audio"])
async def test_over_cap_is_refused_without_reading_the_part(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
    kind, filename,
):
    """The over-cap rejection happens BEFORE the part is buffered into memory — the
    branch helper is never reached. (Audio is excluded: it streams + caps itself,
    so 'before read' is its streaming path, not the branch dispatch.)"""
    import routes.files_mcp_upload as mod

    pid, doc_id, admin_uid = project_with_doc
    _patch_small_caps(monkeypatch, mb=1)
    key = await _make_agent_key(test_db, admin_uid, pid)

    mime = _mime_for(kind)
    from files_util import classify_upload_kind

    cap_mb = (await classify_upload_kind(filename, mime))[1]
    over = cap_mb * 1024 * 1024 + 4096

    mint = await _mint(client, key, attach_to=doc_id, filename=filename)

    reached = {"n": 0}
    branch = {"docx": "_store_docx", "markdown": "_store_markdown", "image": "_store_image"}[kind]

    def _spy(*a, **kw):
        reached["n"] += 1
        raise AssertionError("branch reached before the cap check")

    monkeypatch.setattr(mod, branch, _spy)
    resp = await client.post(
        mint["url"], files={"file": (filename, _over_payload(kind, over), mime)},
    )
    assert resp.status_code == 413, resp.text
    assert reached["n"] == 0, "the part was buffered into memory before the cap rejected it"


def test_size_cap_helper_decides_none_explicitly():
    """Unit-level pin for the None-size decision: audio defers (streaming caps),
    the buffered kinds are refused (the read cannot be bounded)."""
    from routes.files_mcp_upload import _enforce_size_cap

    class _F:
        size = None

    # Audio: no pre-rejection (save_audio_upload streams + caps).
    _enforce_size_cap(_F(), "audio", 1)
    # Buffered kinds: refused — a None size cannot be proven under the cap, and
    # the buffered branch would pull the whole part into memory.
    for kind in ("docx", "markdown", "image"):
        with pytest.raises(Exception):  # noqa: PT011 — any HTTPException
            _enforce_size_cap(_F(), kind, 1)


# ═══ D-B — a deduplicated replay reports the node's REAL processing status ═════


@pytest.mark.asyncio
async def test_replay_reports_real_processing_status(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A replay of the SAME url returns the stored node's ACTUAL processing_status
    (asserted over the DB row, not a literal). Pre-fix the replay returned a
    hardcoded None — terminal in this surface — so a retried audio POST lied that
    the transcript was done while still queued."""
    from db import fetch_one

    pid, doc_id, admin_uid = project_with_doc
    pin_stt_url(monkeypatch, "")
    key = await _make_agent_key(test_db, admin_uid, pid)

    mint = await _mint(client, key, attach_to=doc_id, filename="once.wav")
    wav = _wav_bytes(256)
    r1 = await client.post(mint["url"], files={"file": ("once.wav", wav, "audio/wav")})
    assert r1.status_code == 200, r1.text
    node_id = r1.json()["document_id"]
    stored = await fetch_one("documents", node_id)

    r2 = await client.post(mint["url"], files={"file": ("once.wav", wav, "audio/wav")})
    assert r2.status_code == 200, r2.text
    replay = r2.json()
    assert replay["deduplicated"] is True
    # The replay reports the SAME status the stored node actually holds.
    assert replay["processing_status"] == stored.get("processing_status"), replay
    assert replay["media_type"] == stored.get("media_type")
    assert replay["title"] == stored.get("title")


@pytest.mark.asyncio
async def test_replay_of_deleted_node_is_404(
    client, mcp_running, test_db, admin_user, project_with_doc, monkeypatch,
):
    """If the node was removed between the two POSTs, the honest answer is not a
    replay of an id that resolves to nothing — return 404."""
    from db import get_db

    pid, doc_id, admin_uid = project_with_doc
    pin_stt_url(monkeypatch, "")
    key = await _make_agent_key(test_db, admin_uid, pid)

    mint = await _mint(client, key, attach_to=doc_id, filename="gone.wav")
    wav = _wav_bytes(256)
    r1 = await client.post(mint["url"], files={"file": ("gone.wav", wav, "audio/wav")})
    node_id = r1.json()["document_id"]
    # Soft-delete the node between the two POSTs.
    await (await get_db()).query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": node_id},
    )
    r2 = await client.post(mint["url"], files={"file": ("gone.wav", wav, "audio/wav")})
    assert r2.status_code == 404, r2.text


# ═══ D-C — the remote-image guard moves to the authored-input boundary ════════


@pytest.mark.asyncio
async def test_route_document_content_keeps_a_preexisting_remote_image(
    client, test_db, admin_user, project_with_doc,
):
    """Root cause of D-C: route_document_content converges a WHOLE buffer, so it
    must NOT scan it for remote images — a buffer can legitimately carry an image a
    human already typed (the note-link strip rewrites exactly such a buffer)."""
    from db import fetch_one

    pid, doc_id, _admin_uid = project_with_doc
    from agent.doc_state import route_document_content

    await route_document_content(
        doc_id=doc_id, new_content="![pic](https://example.com/x.png)\n", project_id=pid,
    )
    row = await fetch_one("documents", doc_id)
    assert "https://example.com/x.png" in row["content"], row["content"]


@pytest.mark.asyncio
async def test_note_link_strip_survives_a_remote_image_in_the_document(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """THE silent-degradation bug: a document holding a remote image URL (typed by a
    human) + a note link. Deleting the note strips the link. Pre-fix, the strip ran
    through route_document_content, whose remote-image scan raised 400 inside the
    best-effort except → the link silently stayed. Drives the REAL convergence path
    (no mock on route_document_content)."""
    from db import create_record, fetch_one

    pid, host_id, admin_uid = project_with_doc
    sid = f"rf-note-{secrets.token_hex(4)}"
    # A host doc whose content holds a remote image AND the note link.
    host = f"rf-host-{secrets.token_hex(4)}"
    await create_record("documents", host, {
        "project_id": pid, "parent_id": host_id, "title": "Host", "content": "",
        "path": "host.md", "is_index": False, "is_reference": False,
    })
    from ydoc_store import set_content

    seeded = f"![pic](https://example.com/x.png)\n[L](note:{sid})\n"
    await set_content(host, seeded, persist=True)

    session = {"id": sid, "is_note": True, "document_id": host, "project_id": pid}

    async def fake_require(_sid, _user):
        return session

    async def _drain():
        from chat_sessions.note_events import _emit_tasks

        if _emit_tasks:
            await asyncio.gather(*_emit_tasks, return_exceptions=True)

    monkeypatch.setattr("routes.chat.sessions._require_session_access", fake_require)
    from routes.chat.sessions import delete_session

    await delete_session(sid, {"user_id": admin_uid}, db=await routes.chat.sessions.get_db())
    await _drain()

    row = await fetch_one("documents", host)
    assert f"note:{sid}" not in row["content"], row["content"]
    # The pre-existing remote image survives (the strip only touched the note link).
    assert "https://example.com/x.png" in row["content"], row["content"]


@pytest.mark.asyncio
async def test_append_to_empty_doc_with_remote_image_is_rejected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Empty-doc append (the content-set branch) rejects a remote image in the
    AUTHORED append text. Regression guard: removing the scan from
    route_document_content must NOT open this path — the guard moves to the append
    entry point."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    created = _result_text(await _call(client, key, "create_document", {
        "parent_id": doc_id, "node_type": "reference",
        "title": "Empty", "content": "",
    }))
    target = created.get("document_id") or created.get("doc_id")
    data = await _call(client, key, "append_to_document", {
        "document_id": target,
        "content": "![pic](https://example.invalid/x.png)\n",
    })
    assert _is_error(data), data
    assert _result_text(data)["status_code"] == 400


@pytest.mark.asyncio
async def test_append_to_nonempty_doc_with_remote_image_is_rejected(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Non-empty append (the synthesized-edit branch) rejects a remote image too —
    proving the guard covers BOTH branches, not only the one that used to be covered
    by route_document_content. This branch is guarded by route_document_edits' scan
    over new_text (the correct, unchanged boundary)."""
    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    created = _result_text(await _call(client, key, "create_document", {
        "parent_id": doc_id, "node_type": "reference",
        "title": "Filled", "content": "Existing body.\n",
    }))
    target = created.get("document_id") or created.get("doc_id")
    data = await _call(client, key, "append_to_document", {
        "document_id": target,
        "content": "\n![pic](https://example.invalid/x.png)\n",
    })
    assert _is_error(data), data
    assert _result_text(data)["status_code"] == 400


# ═══ D-D — the mint refuses a host that cannot be a parent ═════════════════════


@pytest.mark.asyncio
async def test_attach_file_to_a_reference_host_is_refused_at_mint(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """A reference node cannot be a parent (schema documents_parent_check). The
    refusal must land at MINT — no url, no upload token issued — so the client never
    streams a file to a dead URL. Asserting the MINT refuses is the fix; asserting
    the redeem 500s is the bug."""
    from db import create_record

    pid, doc_id, admin_uid = project_with_doc
    key = await _make_agent_key(test_db, admin_uid, pid)
    # A reference node (looks exactly like a host id to a weak model).
    ref_id = f"rf-ref-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": doc_id, "title": "ref", "content": "",
        "path": f"{ref_id}.md", "is_index": False, "is_reference": True,
        "media_type": "image", "file_path": "img/x.png",
    })
    data = await _call(client, key, "attach_file", {
        "attach_to": ref_id, "filename": "probe.png",
    })
    assert _is_error(data), data
    payload = _result_text(data)
    assert payload["status_code"] == 400, payload
    # No url / accepted in the response — the mint refused before issuing a token.
    assert "url" not in payload
    assert payload.get("accepted") is not True
    # The message names the fix in the surface's own vocabulary (a file attaches to
    # a document, not to another attached node).
    assert "document" in payload.get("error", "").lower(), payload
