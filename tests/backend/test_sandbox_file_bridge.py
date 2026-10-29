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

import base64
import hashlib
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


# ════════════════════════════════════════════════════════════════════════════
# A. Structural path validation — pure function, no SSH
# INVARIANT(security): the workspace root comes from the KEY; the argument may
# only name a LEAF beneath it. Both layers are required (backend structural +
# sandbox realpath).
# ════════════════════════════════════════════════════════════════════════════


_WS = "/workspace/abcd0123"


def test_leaf_accepts_absolute_inside_workspace_prefix_stripped():
    """An absolute path INSIDE the workspace is accepted with the prefix stripped.
    Why: the shell hands the agent absolute paths (pwd, ls, os.path.abspath), and
    refusing the very string the system just printed reads as a bug and drives a
    retry loop. The root still comes from the KEY — it is COMPARED to the argument,
    never taken from it."""
    from routes.tool_api.sandbox.transport import _validate_workspace_leaf

    leaf = _validate_workspace_leaf(f"{_WS}/sub/chart.png", _WS)
    assert leaf == "sub/chart.png"


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


# ════════════════════════════════════════════════════════════════════════════
# E. Registration — route / tool surface / MCP projection / repeat bound / chip
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# F. Category coupling — description categories == detect_media_type categories
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# G. Tool descriptions name sandbox_path on sandbox_bash
# ════════════════════════════════════════════════════════════════════════════


# ─── Test-internal helpers ───────────────────────────────────────────────────


def _noop_async(retval):
    async def _fn(*args, **kwargs):
        return retval
    return _fn


def _workspace_for(user_id: str) -> str:
    from routes.tool_api.sandbox.transport import workspace_for
    return workspace_for(user_id)
