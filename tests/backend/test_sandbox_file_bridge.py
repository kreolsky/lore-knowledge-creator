"""Contract tests for the sandbox file bridge (plan sandbox-file-bridge).

Two directions over SFTP (a binary channel that bypasses the MAX_OUTPUT_CHARS stdout
cap on `sandbox_bash`):

  OUTBOUND — `sandbox_path`: a 4th input form on upload_document / upload_reference
    that names a file already in the agent's workspace. The backend reads it via SFTP
    and funnels the bytes through the EXISTING import pipeline. Outbound path
    validation is TWO-LAYERED: a structural check (reject .., absolute-outside, empty)
    on the backend, and a realpath prefix check on the sandbox (a symlink planted in
    the workspace that resolves to /etc/passwd must be caught — sftp.realpath
    RESOLVES and returns the escaped path rather than refusing it).

  INBOUND — `sandbox_fetch_reference`: copies an already-uploaded reference INTO the
    workspace at a deterministic path `{ws}/inbox/{ref_id}/{safe_name}`. The source
    path is READ from the reference row (never reconstructed from an agent filename),
    and the reference must belong to the acting project (cross-project fetch is a
    breach).

Both directions are gated EXACTLY like the console: the internal whole-project key
+ full project access. The outbound gate is PER-FORM (rides on read_workspace_file,
fires only when sandbox_path is set) so the content_base64 form — which legitimately
serves external MCP and subtree-scoped keys — keeps working.

# ARCH: the SSH SFTP operations are the seams (`_sftp_stat`, `_sftp_read`,
# `_sftp_write`), monkeypatched here so the security predicates are provable in CI
# where no sandbox host exists (same strategy as test_sandbox_tool's `_ssh_exec`).
"""

import ast
import base64
import hashlib
import inspect
import secrets

import pytest

# ─── Shared helpers (mirror test_sandbox_tool / test_upload_tools) ────────────


async def _make_key(
    user_id: str, project_id: str, *,
    internal: bool, document_id: str = "",
    capabilities: tuple[str, ...] = ("agent",),
) -> str:
    """Insert an api_keys row and return the plaintext token."""
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"fb-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent",
        "capabilities": list(capabilities),
        "internal": internal,
    })
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# 1x1 PNG bytes (magic-valid) — for the binary image paths.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
# Minimal valid DOCX magic: a ZIP local-file-header signature.
DOCX_MAGIC = b"PK\x03\x04" + b"\x00" * 64


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
    """Replace the SFTP seams with recorders. Returns a dict of call lists +
    mutable return values so individual tests can dial behavior.

    The read path is ONE merged seam (`_sftp_read_checked`: realpath + bounded read
    on one channel) — mirroring the implementation, which stat+read together to close
    the TOCTOU window a two-channel split would leave open."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    read_calls: list[str] = []
    write_calls: list[dict] = []

    # Mutable knobs a test can override before the call.
    state = {
        "realpath": None,   # defaults to the requested path (no symlink)
        "data": PNG_BYTES,
    }

    async def _fake_read_checked(ws_path: str, max_bytes: int):
        read_calls.append(ws_path)
        return state["realpath"] or ws_path, state["data"]

    async def _fake_write(ws_path: str, data: bytes):
        write_calls.append({"path": ws_path, "size": len(data)})

    monkeypatch.setattr(sandbox.files, "_sftp_read_checked", _fake_read_checked)
    monkeypatch.setattr(sandbox.files, "_sftp_write", _fake_write)
    return {
        "read_calls": read_calls, "write_calls": write_calls, "state": state,
    }


async def _member(user_id: str, project_id: str, access_level: str) -> None:
    from db import create_record

    await create_record("users", user_id, {
        "email": f"{user_id}@sb.test", "name": user_id, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"fb-pm-{secrets.token_hex(4)}", {
        "project_id": project_id, "user_id": user_id, "access_level": access_level,
    })


async def _make_doc(client, token, project_id, title, content=""):
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _read_doc(client, token, doc_id):
    resp = await client.get(
        f"/api/documents/{doc_id}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ════════════════════════════════════════════════════════════════════════════
# A. Structural path validation — pure function, no SSH
# INVARIANT(security): the workspace root comes from the KEY; the argument may
# only name a LEAF beneath it. Both layers are required (backend structural +
# sandbox realpath).
# ════════════════════════════════════════════════════════════════════════════


_WS = "/workspace/abcd0123"


def test_leaf_rejects_empty():
    from fastapi import HTTPException
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    for empty in ("", "   ", None):
        with pytest.raises(HTTPException) as exc:
            _validate_workspace_leaf(empty, _WS)
        assert exc.value.status_code == 400


def test_leaf_rejects_dotdot():
    """A `..` segment that escapes the workspace is rejected structurally — the
    backend layer must not rely on the far-end realpath check alone."""
    from fastapi import HTTPException
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    for hostile in ("../../etc/passwd", "..", "a/../../b", "sub/../../etc"):
        with pytest.raises(HTTPException) as exc:
            _validate_workspace_leaf(hostile, _WS)
        assert exc.value.status_code == 400, f"accepted hostile path: {hostile}"


def test_leaf_accepts_inward_normalization():
    """`a/../b` stays INSIDE the workspace (normalizes to `b`) — only escaping `..`
    is rejected. A heavy hand here would refuse legitimate relative navigation."""
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    assert _validate_workspace_leaf("a/../b.png", _WS) == "b.png"
    assert _validate_workspace_leaf("./chart.png", _WS) == "chart.png"


def test_leaf_rejects_absolute_outside_workspace():
    from fastapi import HTTPException
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    with pytest.raises(HTTPException) as exc:
        _validate_workspace_leaf("/etc/passwd", _WS)
    assert exc.value.status_code == 400


def test_leaf_accepts_absolute_inside_workspace_prefix_stripped():
    """An absolute path INSIDE the workspace is accepted with the prefix stripped.
    Why: the shell hands the agent absolute paths (pwd, ls, os.path.abspath), and
    refusing the very string the system just printed reads as a bug and drives a
    retry loop. The root still comes from the KEY — it is COMPARED to the argument,
    never taken from it."""
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    leaf = _validate_workspace_leaf(f"{_WS}/sub/chart.png", _WS)
    assert leaf == "sub/chart.png"


def test_leaf_rejects_prefix_collision_attack():
    """`/workspace/abcd0123../evil` shares a prefix STRING with the workspace but is
    NOT beneath it (no path separator after the digest). The prefix-strip must
    require the trailing slash, or this payload escapes."""
    from fastapi import HTTPException
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    with pytest.raises(HTTPException) as exc:
        _validate_workspace_leaf(f"{_WS}../evil", _WS)
    assert exc.value.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# B. read_workspace_file — the per-form console gate + SFTP layer checks
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_read_workspace_file_gate_rides_on_primitive(monkeypatch, sandbox_configured):
    """read_workspace_file itself calls _require_console_key — so a non-internal key
    is rejected at the primitive, not at the endpoint. Why here: the gate then
    cannot be forgotten by a future second caller of the primitive."""
    from fastapi import HTTPException
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    monkeypatch.setattr(sandbox.files, "_sftp_read_checked", _noop_async(("", b"")))

    # A non-internal ctx (external MCP / user-minted key shape).
    ctx = {"internal": None, "scope_root": "", "user_id": "u1"}
    with pytest.raises(HTTPException) as exc:
        await sandbox.files.read_workspace_file(ctx, "x.png", max_bytes=1000)
    assert exc.value.status_code == 403

    # A subtree-scoped internal key is also rejected.
    ctx = {"internal": True, "scope_root": "some-doc", "user_id": "u1"}
    with pytest.raises(HTTPException) as exc:
        await sandbox.files.read_workspace_file(ctx, "x.png", max_bytes=1000)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_read_workspace_file_rejects_symlink_escape(sftp_seams):
    """A symlink planted in the workspace that resolves outside it (e.g. to
    /etc/passwd) must be caught by the realpath prefix check.

    Note: sftp.realpath RESOLVES and returns the escaped path (it does NOT refuse
    it), and it succeeds on paths that do not exist — so the prefix comparison, not
    the realpath call, is the enforcement. Verified live in the plan's Evidence.
    The read is bounded (max_bytes + 1), so even though bytes are read before the
    prefix check, an escape can neither OOM nor return data."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    ctx = {"internal": True, "scope_root": "", "user_id": "u1"}
    sftp_seams["state"]["realpath"] = "/etc/passwd"  # symlink resolved outside
    with pytest.raises(Exception) as exc:
        await sandbox.files.read_workspace_file(ctx, "link.png", max_bytes=1000)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_read_workspace_file_rejects_oversize_via_bounded_read(sftp_seams):
    """The size cap is AUTHORITATIVE on len(data): the read is bounded to max_bytes+1,
    so a file larger than the cap (read mid-growth or otherwise) yields at most
    max_bytes+1 bytes and is rejected. This closes the TOCTOU a stat-then-read split
    on two channels would leave open — the sandbox is adversarial and can grow a file
    between checks."""
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    ctx = {"internal": True, "scope_root": "", "user_id": "u1"}
    # Simulate the seam returning more than the cap (a grew-mid-read or oversize file).
    sftp_seams["state"]["data"] = b"\x00" * 2000
    with pytest.raises(Exception) as exc:
        await sandbox.files.read_workspace_file(ctx, "big.png", max_bytes=1000)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_read_workspace_file_returns_bytes_happy_path(sftp_seams):
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    ctx = {"internal": True, "scope_root": "", "user_id": "u1"}
    sftp_seams["state"]["data"] = b"hello-bytes"
    data = await sandbox.files.read_workspace_file(ctx, "chart.png", max_bytes=1000)
    assert data == b"hello-bytes"


# ════════════════════════════════════════════════════════════════════════════
# C. OUTBOUND — import_file with sandbox_path (node_type picks doc vs reference)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_png_creates_reference(
    client, test_db, project_with_doc, sftp_seams,
):
    """sandbox_path → SFTP read → existing binary save path → an image reference."""
    pid, _, uid = project_with_doc
    from db import create_record

    # A host doc for the reference.
    host = f"fb-host-{secrets.token_hex(4)}"
    await create_record("documents", host, {
        "project_id": pid, "parent_id": None, "title": "Host", "content": "",
        "path": f"_doc/{host}.md", "is_index": False,
    })
    token = await _make_key(uid, pid, internal=True)
    sftp_seams["state"]["data"] = PNG_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "chart.png", "node_type": "reference",
        "sandbox_path": "outputs/chart.png", "parent_id": host,
    }, headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    # The reference row exists and is an image.
    rows = await test_db.query(
        "SELECT is_reference, media_type, parent_id FROM type::record('documents', $id)",
        {"id": data["reference_id"]},
    )
    assert rows[0]["is_reference"] is True
    assert rows[0]["media_type"] == "image"
    assert rows[0]["parent_id"] == host


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_non_internal_key(
    client, test_db, project_with_doc, sftp_seams,
):
    """The per-form console gate: a non-internal key (the shape external MCP keys
    and ordinary user-minted keys take) cannot use sandbox_path."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=False)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "chart.png", "node_type": "reference",
        "sandbox_path": "chart.png", "parent_id": "host",
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["read_calls"] == [], "rejected key reached SFTP"


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_subtree_key(
    client, test_db, project_with_doc, sftp_seams,
):
    """A subtree-scoped internal key (document_id != '') cannot use sandbox_path —
    a key deliberately scoped to one document tree must not silently widen into the
    console."""
    pid, idx, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True, document_id=idx)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "chart.png", "node_type": "reference",
        "sandbox_path": "chart.png", "parent_id": idx,
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["read_calls"] == []


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_commentator(
    client, test_db, project_with_doc, sftp_seams,
):
    """Below full project access, sandbox_path is rejected — even with a valid
    internal key. The console gate is TWO checks: key identity AND access level."""
    pid, _, _ = project_with_doc
    guest = f"fb-guest-{secrets.token_hex(4)}"
    await _member(guest, pid, "commentator")
    token = await _make_key(guest, pid, internal=True)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "chart.png", "node_type": "reference",
        "sandbox_path": "chart.png", "parent_id": "host",
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["read_calls"] == []


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_dotdot(
    client, test_db, project_with_doc, sftp_seams,
):
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "chart.png", "node_type": "reference",
        "sandbox_path": "../../etc/passwd", "parent_id": "host",
    }, headers=_hdr(token))
    assert resp.status_code == 400, resp.text
    assert sftp_seams["read_calls"] == []


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_symlink_escape(
    client, test_db, project_with_doc, sftp_seams,
):
    """End-to-end: the SFTP seam reports a realpath outside the workspace → 403,
    and no reference is created."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    sftp_seams["state"]["realpath"] = "/etc/passwd"

    resp = await client.post("/api/tool/import_file", json={
        "filename": "chart.png", "node_type": "reference",
        "sandbox_path": "link.png", "parent_id": "host",
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_oversize(
    client, test_db, project_with_doc, sftp_seams, monkeypatch,
):
    """The size cap fires on the outbound side using the media type's existing cap
    (no new constants). With the cap zeroed, any returned bytes exceed it → 413."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    # Zero the image cap so the fixture's default PNG payload (67 bytes) is "oversize".
    monkeypatch.setattr("config.MAX_IMAGE_SIZE_MB", 0)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "big.png", "node_type": "reference",
        "sandbox_path": "big.png", "parent_id": "host",
    }, headers=_hdr(token))
    assert resp.status_code == 413, resp.text


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_rejects_unsupported_binary(
    client, test_db, project_with_doc, sftp_seams,
):
    """A binary whose extension is neither image nor audio nor docx is rejected
    (400) — the category limit surfaces at the point of failure. Uses real binary
    bytes (a NUL byte fails the is_text_bytes gate) so the .pdf routes to the binary
    path and hits the unsupported-type check, not the text path."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    sftp_seams["state"]["data"] = b"\x00\x01\x02\x03PDF-binary-junk"

    resp = await client.post("/api/tool/import_file", json={
        "filename": "report.pdf", "node_type": "reference",
        "sandbox_path": "report.pdf", "parent_id": "host",
    }, headers=_hdr(token))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_upload_reference_sandbox_path_docx(
    client, test_db, project_with_doc, sftp_seams, monkeypatch,
):
    """A .docx in the workspace → SFTP read → docx converter → a markdown reference."""
    pid, _, uid = project_with_doc
    from db import create_record

    host = f"fb-host-{secrets.token_hex(4)}"
    await create_record("documents", host, {
        "project_id": pid, "parent_id": None, "title": "Host", "content": "",
        "path": f"_doc/{host}.md", "is_index": False,
    })
    token = await _make_key(uid, pid, internal=True)
    sftp_seams["state"]["data"] = DOCX_MAGIC

    from unittest.mock import AsyncMock, patch
    with patch("routes.tool_api.imports.post_docx_to_converter",
               new_callable=AsyncMock, return_value="# From docx\n\nBody.\n"):
        resp = await client.post("/api/tool/import_file", json={
            "filename": "report.docx", "node_type": "reference",
            "sandbox_path": "report.docx", "parent_id": host,
        }, headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"


@pytest.mark.asyncio
async def test_upload_document_sandbox_path_rejects_binary_image(
    client, test_db, project_with_doc, sftp_seams,
):
    """An image is never a document — import_file(node_type="document") rejects
    binary even via sandbox_path, naming `node_type: "reference"` as the fix."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)
    sftp_seams["state"]["data"] = PNG_BYTES

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "sandbox_path": "pic.png",
    }, headers=_hdr(token))
    assert resp.status_code == 400, resp.text
    assert "node_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_document_sandbox_path_md_creates_document(
    client, test_db, project_with_doc, sftp_seams, admin_user,
):
    """A markdown file in the workspace → upload_document → a new document."""
    pid, _, uid = project_with_doc
    _, token = admin_user
    agent_tok = await _make_key(uid, pid, internal=True)
    sftp_seams["state"]["data"] = b"# Title\n\nSome body text.\n"

    resp = await client.post("/api/tool/import_file", json={
        "filename": "notes.md", "sandbox_path": "notes.md",
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    # The document content landed.
    rd = await client.get(
        f"/api/documents/{data['doc_id']}", cookies={"lore_session": token},
    )
    assert "Some body text" in rd.json().get("content", "")


@pytest.mark.asyncio
async def test_upload_sandbox_path_xor_with_other_forms(
    client, test_db, project_with_doc, sftp_seams,
):
    """sandbox_path is mutually exclusive with attachment_index (D5: content_base64
    left the Pi surface; D2: plain `content` is no longer a form — authored text
    goes to create_document)."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    # sandbox_path + attachment_index → 422.
    resp = await client.post("/api/tool/import_file", json={
        "filename": "x.png", "node_type": "reference", "parent_id": "h",
        "attachment_index": 0,
        "sandbox_path": "x.png",
    }, headers=_hdr(token))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_upload_document_sandbox_path_rejects_non_internal_key(
    client, test_db, project_with_doc, sftp_seams,
):
    """Per-form gate applies to upload_document too — the shared _import_document
    core resolves sandbox_path the same way."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=False)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "x.md", "sandbox_path": "x.md",
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["read_calls"] == []


# ════════════════════════════════════════════════════════════════════════════
# D. INBOUND — sandbox_fetch_reference
# ════════════════════════════════════════════════════════════════════════════


async def _seed_reference_on_disk(
    test_db, project_id, ref_id, safe_name, data,
):
    """Persist a reference row with a stored file_path AND the bytes on disk under
    STORAGE_PATH, matching save_upload's layout ({project_id}/{ref_id}/{safe_name})."""
    from config import STORAGE_PATH
    from db import create_record

    rel = f"{project_id}/{ref_id}/{safe_name}"
    disk = STORAGE_PATH / project_id / ref_id / safe_name
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(data)
    # Reference-host invariant: attach the ref to a real host doc (idempotent per project).
    host_id = f"{project_id}-host"
    try:
        await create_record("documents", host_id, {
            "project_id": project_id, "parent_id": None, "title": "Host",
            "content": "", "path": f"{host_id}.md", "is_index": False,
        })
    except RuntimeError:
        pass
    await create_record("documents", ref_id, {
        "project_id": project_id, "parent_id": host_id, "title": safe_name,
        "content": "", "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": "image",
        "file_path": rel,
        "file_meta": {"mime_type": "image/png", "file_size": len(data),
                      "original_name": safe_name},
    })
    return rel, disk


@pytest.mark.asyncio
async def test_fetch_reference_writes_to_deterministic_inbox(
    client, test_db, project_with_doc, sftp_seams,
):
    """A reference in the acting project is copied into the workspace at
    {ws}/inbox/{ref_id}/{safe_name}. The source path is READ from the reference row
    (file_path), never reconstructed from an agent filename — the agent holds only
    ref_id and cannot know safe_name."""
    pid, _, uid = project_with_doc
    ref_id = f"fb-ref-{secrets.token_hex(4)}"
    safe = "chart.png"
    await _seed_reference_on_disk(test_db, pid, ref_id, safe, PNG_BYTES)
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": ref_id,
    }, headers=_hdr(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"

    ws = _workspace_for(uid)
    expected = f"{ws}/inbox/{ref_id}/{safe}"
    assert len(sftp_seams["write_calls"]) == 1
    assert sftp_seams["write_calls"][0]["path"] == expected
    # The bytes written are the reference's stored bytes.
    assert sftp_seams["write_calls"][0]["size"] == len(PNG_BYTES)


@pytest.mark.asyncio
async def test_fetch_reference_rejects_foreign_project(
    client, test_db, project_with_doc, sftp_seams,
):
    """Inbound must verify the reference belongs to the acting project — without it
    an agent keyed to project A fetches a reference from project B."""
    pid, _, uid = project_with_doc
    # A reference in a DIFFERENT project.
    other_pid = f"fb-other-{secrets.token_hex(4)}"
    await test_db.query(
        "CREATE type::record('projects', $id) SET name='Other', status='active', "
        "project_context='', owner_id=$uid",
        {"id": other_pid, "uid": uid},
    )
    ref_id = f"fb-foreign-{secrets.token_hex(4)}"
    await _seed_reference_on_disk(test_db, other_pid, ref_id, "x.png", PNG_BYTES)
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": ref_id,
    }, headers=_hdr(token))
    assert resp.status_code in (403, 404), resp.text
    assert sftp_seams["write_calls"] == [], "wrote a foreign-project reference"


@pytest.mark.asyncio
async def test_fetch_reference_rejects_non_internal_key(
    client, test_db, project_with_doc, sftp_seams,
):
    pid, _, uid = project_with_doc
    ref_id = f"fb-ref-{secrets.token_hex(4)}"
    await _seed_reference_on_disk(test_db, pid, ref_id, "x.png", PNG_BYTES)
    token = await _make_key(uid, pid, internal=False)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": ref_id,
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["write_calls"] == []


@pytest.mark.asyncio
async def test_fetch_reference_rejects_subtree_key(
    client, test_db, project_with_doc, sftp_seams,
):
    pid, idx, uid = project_with_doc
    ref_id = f"fb-ref-{secrets.token_hex(4)}"
    await _seed_reference_on_disk(test_db, pid, ref_id, "x.png", PNG_BYTES)
    token = await _make_key(uid, pid, internal=True, document_id=idx)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": ref_id,
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["write_calls"] == []


@pytest.mark.asyncio
async def test_fetch_reference_rejects_commentator(
    client, test_db, project_with_doc, sftp_seams,
):
    pid, _, _ = project_with_doc
    ref_id = f"fb-ref-{secrets.token_hex(4)}"
    await _seed_reference_on_disk(test_db, pid, ref_id, "x.png", PNG_BYTES)
    guest = f"fb-guest-{secrets.token_hex(4)}"
    await _member(guest, pid, "commentator")
    token = await _make_key(guest, pid, internal=True)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": ref_id,
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["write_calls"] == []


@pytest.mark.asyncio
async def test_fetch_reference_rejects_unknown_ref(
    client, test_db, project_with_doc, sftp_seams,
):
    """A non-existent ref_id → 404 (no existence oracle / uniform not-found)."""
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=True)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": "does-not-exist-xyz",
    }, headers=_hdr(token))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_fetch_reference_rejects_non_reference_doc(
    client, test_db, project_with_doc, sftp_seams, admin_user,
):
    """A document id that is NOT a reference (is_reference=false) → 404 — the tool
    fetches references, not documents."""
    pid, _, uid = project_with_doc
    _, token = admin_user
    doc_id = await _make_doc(client, token, pid, "PlainDoc", "body")
    agent_tok = await _make_key(uid, pid, internal=True)

    resp = await client.post("/api/tool/sandbox_fetch_reference", json={
        "ref_id": doc_id,
    }, headers=_hdr(agent_tok))
    assert resp.status_code == 404, resp.text


# ════════════════════════════════════════════════════════════════════════════
# E. Registration — route / tool surface / MCP projection / repeat bound / chip
# ════════════════════════════════════════════════════════════════════════════


async def test_fetch_reference_route_matches_tool_name(sandbox_configured):
    """The route path MUST equal the tool name exactly (INVARIANT sandbox.py:294) —
    the Pi driver builds the URL generically and consults no path map."""
    from agent.tools import agent_toolset

    from main import app

    tool_paths = {
        r.path for r in app.routes
        if getattr(r, "path", "").startswith("/api/tool/")
        and "POST" in getattr(r, "methods", set())
    }
    missing = [
        t["function"]["name"] for t in await agent_toolset()
        if f"/api/tool/{t['function']['name']}" not in tool_paths
    ]
    assert not missing, f"served tools with no route: {missing}"
    assert "/api/tool/sandbox_fetch_reference" in tool_paths


async def test_fetch_reference_served_on_agent_path(sandbox_configured):
    from agent.tools import agent_toolset

    names = {t["function"]["name"] for t in await agent_toolset()}
    assert "sandbox_fetch_reference" in names


def test_fetch_reference_absent_from_mcp():
    """sandbox_fetch_reference is console-only — it must not be served to MCP at all
    (a stronger statement than dropping a property). An external MCP key cannot run
    sandbox_bash, so its workspace never exists — exposing a read form to it would
    widen the internal-only console perimeter over a directory that is always empty."""
    from mcp_gateway.schemas import build_tool_list

    assert "sandbox_fetch_reference" not in {t.name for t in build_tool_list()}


def test_fetch_reference_is_mutating():
    """Mutating for the SAME reason sandbox_bash is: sequential execution in the Pi
    driver, so two concurrent fetches cannot race over one workspace directory."""
    from agent.tools import MUTATING_TOOLS

    assert "sandbox_fetch_reference" in MUTATING_TOOLS


async def test_import_file_is_agent_only_not_on_mcp_surface():
    """D14 (plan mcp-tool-surface-redesign): import_file left the MCP surface (agent-only
    now — sandbox_path / attachment_index stay because the driver reads the file, so
    bytes never pass through the model). There is no mcp_hidden projection to test
    anymore; this asserts the tool is simply absent from the MCP advertised surface
    while the Pi tool still carries its console-only inputs."""
    from agent.tools import agent_toolset
    from agent_tools.specs.media import IMPORT_FILE_TOOL
    from mcp_gateway.schemas import build_tool_list

    advertised = {t.name for t in build_tool_list()}
    assert "import_file" not in advertised
    src_props = IMPORT_FILE_TOOL["function"]["parameters"]["properties"]
    assert "sandbox_path" in src_props
    assert "attachment_index" in src_props
    assert "import_file" in {t["function"]["name"] for t in await agent_toolset()}


# ════════════════════════════════════════════════════════════════════════════
# F. Category coupling — description categories == detect_media_type categories
# ════════════════════════════════════════════════════════════════════════════


def test_upload_description_categories_match_detect_media_type():
    """The upload descriptions route binary by CATEGORY (images / audio / text /
    docx), not by enumerating members. This test guards the one coupling that CAN
    drift: the set of categories `detect_media_type` can return (derived from its
    source via AST) must be a subset of the categories the descriptions name. It
    fails on a NEW category (e.g. 'video') — which is when the sentence is actually
    wrong — and stays silent when a member is added (e.g. image/avif), which is when
    it is still right."""
    from agent_tools.specs.media import IMPORT_FILE_TOOL
    from files_util import detect_media_type

    # Derive the non-None categories detect_media_type can return by AST-parsing
    # its return statements (not a literal — catches a newly added branch).
    src = inspect.getsource(detect_media_type)
    tree = ast.parse(src)
    returned = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            returned.add(node.value.value)
    assert returned, "could not derive detect_media_type categories from source"

    # D14: import_file is Pi-only (IMPORT_FILE_TOOL). Its description still routes
    # binary by category, so the coupling to detect_media_type's categories holds.
    text = IMPORT_FILE_TOOL["function"]["description"].lower()
    for cat in returned:
        assert cat in text, (
            f"upload description does not name category '{cat}' returned by "
            f"detect_media_type — the routing sentence is wrong for this category"
        )


# ════════════════════════════════════════════════════════════════════════════
# G. Tool descriptions name sandbox_path on sandbox_bash
# ════════════════════════════════════════════════════════════════════════════


def test_sandbox_bash_description_names_sandbox_path():
    """The agent emitted base64 because nothing else was possible. It will keep
    doing so unless the new form is NAMED in the sandbox_bash description (so the
    agent reaches for it, like `restart` is named there)."""
    from agent_tools.specs.media import SANDBOX_TOOL

    desc = SANDBOX_TOOL["function"]["description"].lower()
    assert "sandbox_path" in desc or "sandbox path" in desc, (
        "sandbox_bash description must name sandbox_path so the agent stops "
        "reaching for base64-over-exec"
    )


# ─── Test-internal helpers ───────────────────────────────────────────────────


def _noop_async(retval):
    async def _fn(*args, **kwargs):
        return retval
    return _fn


def _workspace_for(user_id: str) -> str:
    from routes.tool_api.sandbox.transport import workspace_for
    return workspace_for(user_id)
