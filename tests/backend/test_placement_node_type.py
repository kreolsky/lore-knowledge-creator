"""Contract tests for the unified placement + node-kind vocabulary.

Plan agent-document-placement-and-node-type:
  - `parent_id` is the ONLY placement name (create_document's `attach_to` and
    import_file's host `document_id` are deleted — one relation, one word);
  - `node_type` ("document" | "reference") is the ONLY kind name (the
    `is_reference` boolean argument is deleted from the tool surface);
  - create_document: `parent_id` is REQUIRED — omitted = 422 under an unscoped
    key (no silent project-root landing), explicit null = deliberate root;
    under a subtree-scoped key an omitted parent still resolves to the scope
    root (covered by test_subtree_scoped_agent_keys, kept green);
  - move_document: `node_type` omitted = KEEP the kind; passing it CONVERTS the
    node (doc→reference: sort_key cleared, after_id/root rejected, children
    refused; reference→doc: sort_key minted, file-backed refused).

The DB column `is_reference` is NOT part of this rename — rows still carry it,
and the `document_moved` broadcast carries it as the kind flag.
"""

import base64
import hashlib
import secrets

import pytest
from emit_recorder import EmitRecorder

# ─── Helpers (mirror test_append_move_tools) ─────────────────────────────────


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=True,
                          scope_root="", internal=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    key_id = f"nt-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id,
        "document_id": scope_root,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply, "internal": internal,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token, project_id, title, content="", parent_id=None):
    body = {"project_id": project_id, "title": title, "content": content}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body,
                             cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _mk_ref(client, token, project_id, host, title):
    resp = await client.post(
        "/api/references",
        json={"project_id": project_id, "document_id": host, "title": title,
              "media_type": "markdown", "content": "body"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


async def _row(test_db, doc_id):
    rows = await test_db.query(
        "SELECT is_reference, parent_id, sort_key, file_path, media_type, title "
        "FROM type::record('documents', $id)",
        {"id": doc_id},
    )
    assert rows, f"row {doc_id} missing"
    from db import extract_id

    row = dict(rows[0])
    row["parent_id"] = extract_id(row["parent_id"]) if row.get("parent_id") else None
    return row


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def sandbox_seams(monkeypatch):
    """Minimal SFTP read seam so sandbox_path import_file tests run in CI."""
    from helpers import pin_tool_gates
    from routes.tool_api import sandbox
    from routes.tool_api.sandbox import (  # noqa: F401 — submodules own the seams
        bash,
        files,
        lifecycle,
        runs,
        transport,
    )

    pin_tool_gates(monkeypatch, sandbox=True)

    state = {"data": PNG_BYTES}

    async def _fake_read(ws_path: str, max_bytes: int):
        return ws_path, state["data"]

    monkeypatch.setattr(sandbox.files, "_sftp_read_checked", _fake_read)
    return state


# ═══ create_document — placement is required; node_type names the kind ═══════


@pytest.mark.asyncio
async def test_create_document_omitted_placement_422_unscoped(
    client, test_db, admin_user, project_with_doc,
):
    """Omitted parent_id under an UNSCOPED key is a client error — it silently
    landed at the project root before. The 422 names the fix."""
    pid, _, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/create_document", json={
        "title": "NoPlacement", "content": "x", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 422, resp.text
    assert "parent_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_document_explicit_null_parent_is_deliberate_root(
    client, test_db, admin_user, project_with_doc,
):
    """parent_id: null is the DELIBERATE root spelling — presence, not value,
    separates the three cases (omitted / null / id)."""
    pid, _, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/create_document", json={
        "title": "Rooted", "content": "x", "parent_id": None, "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    row = await _row(test_db, resp.json()["doc_id"])
    assert row["parent_id"] is None
    assert row["is_reference"] is False


@pytest.mark.asyncio
async def test_create_document_reference_via_node_type(
    client, test_db, admin_user, project_with_doc,
):
    """node_type: "reference" + parent_id (the host) creates a reference — the
    same one key now names placement AND kind."""
    pid, idx_id, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/create_document", json={
        "title": "RefDoc", "content": "body", "parent_id": idx_id,
        "node_type": "reference", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    row = await _row(test_db, resp.json()["doc_id"])
    assert row["is_reference"] is True
    assert row["parent_id"] == idx_id
    assert row["media_type"] == "markdown"


@pytest.mark.asyncio
async def test_create_document_reference_requires_host(
    client, test_db, admin_user, project_with_doc,
):
    """node_type: "reference" without a parent_id (the host) is a 422 — the
    reference-host invariant is surfaced at the tool edge."""
    pid, _, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/create_document", json={
        "title": "Hostless", "content": "x", "parent_id": None,
        "node_type": "reference", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_document_attach_to_is_gone(
    client, test_db, admin_user, project_with_doc,
):
    """attach_to is DELETED, not aliased — an old-vocabulary call fails with the
    new vocabulary named (no silent absorption by pydantic's extra=ignore)."""
    pid, idx_id, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/create_document", json={
        "title": "OldVocab", "content": "x", "attach_to": idx_id,
        "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "parent_id" in detail
    assert "node_type" in detail


# ═══ import_file — host is parent_id; kind is node_type ══════════════════════


@pytest.mark.asyncio
async def test_import_file_reference_uses_parent_id_host(
    client, test_db, admin_user, project_with_doc, sandbox_seams,
):
    """The host moves from document_id to parent_id; is_reference →
    node_type="reference". One relation, one word, across both create tools."""
    pid, idx_id, admin_uid = project_with_doc
    sandbox_seams["data"] = b"# From sandbox\n\nBody.\n"
    tok = await _make_agent_key(test_db, admin_uid, pid, internal=True)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "note.md", "sandbox_path": "note.md",
        "node_type": "reference", "parent_id": idx_id,
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    row = await _row(test_db, data["doc_id"])
    assert row["is_reference"] is True
    assert row["parent_id"] == idx_id


@pytest.mark.asyncio
async def test_import_file_reference_without_host_422(
    client, test_db, admin_user, project_with_doc, sandbox_seams,
):
    """node_type="reference" with no parent_id (host) fails the cross-field
    validator naming the fix."""
    pid, _, admin_uid = project_with_doc
    sandbox_seams["data"] = b"# From sandbox\n\nBody.\n"
    tok = await _make_agent_key(test_db, admin_uid, pid, internal=True)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "note.md", "sandbox_path": "note.md",
        "node_type": "reference",
    }, headers=_hdr(tok))
    assert resp.status_code == 422, resp.text
    assert "parent_id" in str(resp.json()["detail"])


@pytest.mark.asyncio
async def test_import_file_binary_document_error_names_node_type(
    client, test_db, admin_user, project_with_doc, sandbox_seams,
):
    """A binary imported as a document is refused with the NEW vocabulary —
    the error is the model's self-correction path."""
    pid, _, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid, internal=True)

    resp = await client.post("/api/tool/import_file", json={
        "filename": "pic.png", "sandbox_path": "pic.png",
    }, headers=_hdr(tok))
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "node_type" in detail
    assert "is_reference" not in detail


# ═══ move_document — node_type converts in both directions ═══════════════════


@pytest.mark.asyncio
async def test_move_converts_document_to_reference(
    client, test_db, admin_user, project_with_doc,
):
    """"take it and make it a reference" — one structural op, not a third node:
    is_reference=true, parent=host, sort_key = the TOP key of the host's
    reference group (the old tree key must not survive the conversion — a kept
    key leaves the node in tree sibling queries and it renders twice)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "Host")
    child = await _make_doc(client, token, pid, "Child", parent_id=host)
    # An existing ref on the host pins the ref group: the converted node must
    # land at its TOP (key below the existing ref's key), not keep its tree key.
    existing_ref = await _mk_ref(client, token, pid, host, "Existing")
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": child, "parent_id": host, "node_type": "reference",
        "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    row = await _row(test_db, child)
    assert row["is_reference"] is True
    assert row["parent_id"] == host
    assert isinstance(row["sort_key"], str) and row["sort_key"]
    existing_key = (await _row(test_db, existing_ref))["sort_key"]
    assert row["sort_key"] < existing_key, "conversion lands at the group's top"
    # The node left the tree listing (the project doc list is non-reference).
    listing = await client.get(
        f"/api/projects/{pid}", cookies={"lore_session": token},
    )
    ids = {d["document_id"] for d in listing.json()["documents"]}
    assert child not in ids


@pytest.mark.asyncio
async def test_move_convert_to_reference_rejects_children(
    client, test_db, admin_user, project_with_doc,
):
    """A doc WITH children cannot become a reference — a reference cannot be a
    parent (assert_parent_valid), converting would orphan them. Refused whole,
    not half-applied."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "HostKids")
    parent = await _make_doc(client, token, pid, "Parent")
    await _make_doc(client, token, pid, "Kid", parent_id=parent)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": parent, "parent_id": host,
        "node_type": "reference", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 422, resp.text
    row = await _row(test_db, parent)
    assert row["is_reference"] is False, "refused whole, not half-applied"


@pytest.mark.asyncio
async def test_move_convert_to_reference_rejects_after_id_and_root(
    client, test_db, admin_user, project_with_doc,
):
    """after_id orders tree documents only; a reference cannot sit at the root —
    the same two edges a born reference has, applied to a converted one."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "HostEdges")
    sib = await _make_doc(client, token, pid, "Sib", parent_id=host)
    mover = await _make_doc(client, token, pid, "Mover", parent_id=host)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": mover, "parent_id": host, "after_id": sib,
        "node_type": "reference", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 400, resp.text

    resp = await client.post("/api/tool/move_document", json={
        "document_id": mover, "parent_id": None,
        "node_type": "reference", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_move_converts_reference_to_document(
    client, test_db, admin_user, project_with_doc,
):
    """The return direction: sort_key minted via key_between, the node re-enters
    the tree listing. after_id and a null parent are LEGAL here."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    parent = await _make_doc(client, token, pid, "ReParent")
    ref_resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "BackToDoc", "content": "r",
        "is_reference": True, "media_type": "markdown", "parent_id": parent,
    }, cookies={"lore_session": token})
    ref_id = ref_resp.json()["document_id"]
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": ref_id, "parent_id": parent,
        "node_type": "document", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    row = await _row(test_db, ref_id)
    assert row["is_reference"] is False
    assert row["sort_key"], "a tree document must carry a sort_key"
    listing = await client.get(
        f"/api/projects/{pid}", cookies={"lore_session": token},
    )
    ids = {d["document_id"] for d in listing.json()["documents"]}
    assert ref_id in ids


@pytest.mark.asyncio
async def test_move_convert_rejects_file_backed_reference(
    client, test_db, admin_user, project_with_doc,
):
    """Bytes are not a tree document: a reference with a stored file refuses
    conversion to document. An audio reference (file-backed) is the failure
    actor of the plan's validation."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "AudioHost")
    from db import create_record

    ref_id = f"nt-ref-{secrets.token_hex(4)}"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host, "title": "Voice memo",
        "content": "transcript", "path": f"_ref/{ref_id}.md",
        "is_index": False, "is_reference": True, "media_type": "audio",
        "file_path": f"uploads/{ref_id}.wav",
    })
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": ref_id, "parent_id": host,
        "node_type": "document", "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 422, resp.text
    row = await _row(test_db, ref_id)
    assert row["is_reference"] is True, "refused whole, not half-applied"


@pytest.mark.asyncio
async def test_move_node_type_omitted_preserves_kind(
    client, test_db, admin_user, project_with_doc,
):
    """Omitting node_type KEEPS the kind — a plain move must never convert
    (the pre-conversion behavior for both kinds)."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "PlainHost")
    host2 = await _make_doc(client, token, pid, "PlainHost2")
    ref_resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "PlainRef", "content": "r",
        "is_reference": True, "media_type": "markdown", "parent_id": host,
    }, cookies={"lore_session": token})
    ref_id = ref_resp.json()["document_id"]
    doc = await _make_doc(client, token, pid, "PlainDoc", parent_id=host)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/move_document", json={
        "document_id": ref_id, "parent_id": host2, "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    assert (await _row(test_db, ref_id))["is_reference"] is True

    resp = await client.post("/api/tool/move_document", json={
        "document_id": doc, "parent_id": host2, "apply": "auto",
    }, headers=_hdr(tok))
    assert resp.status_code == 200, resp.text
    assert (await _row(test_db, doc))["is_reference"] is False


# ═══ document_moved broadcast carries the kind ═══════════════════════════════


@pytest.mark.asyncio
async def test_document_moved_broadcast_carries_kind_and_title(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A second client must learn WHICH kind the node now is (else it keeps a
    phantom tree entry until reload) — is_reference + title ride the payload."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    host = await _make_doc(client, token, pid, "EvtHost")
    child = await _make_doc(client, token, pid, "EvtChild", parent_id=host)
    tok = await _make_agent_key(test_db, admin_uid, pid)

    with EmitRecorder.active(passthrough=True) as rec:
        resp = await client.post("/api/tool/move_document", json={
            "document_id": child, "parent_id": host, "node_type": "reference",
            "apply": "auto",
        }, headers=_hdr(tok))
    events = rec.of("document_moved")
    assert resp.status_code == 200, resp.text
    assert events, "document_moved was not emitted"
    moved = events[-1]
    assert moved.get("is_reference") is True
    assert moved.get("title") == "EvtChild"
    assert moved.get("previous_parent_id"), "old host must ride the payload"


# ═══ served schemas speak the new vocabulary only ════════════════════════════


def test_agent_create_document_schema_vocabulary():
    from agent.tools import AGENT_TOOLS

    tool = next(t for t in AGENT_TOOLS
                if t["function"]["name"] == "create_document")
    params = tool["function"]["parameters"]
    props = params["properties"]
    assert "attach_to" not in props, "attach_to is deleted, not aliased"
    assert "is_reference" not in props
    assert props["node_type"]["enum"] == ["document", "reference"]
    assert props["node_type"]["default"] == "document"
    assert set(params["required"]) == {"title", "content", "parent_id"}
    assert props["parent_id"]["type"] == ["string", "null"]


def test_agent_move_document_schema_has_node_type():
    from agent.tools import AGENT_TOOLS

    tool = next(t for t in AGENT_TOOLS
                if t["function"]["name"] == "move_document")
    props = tool["function"]["parameters"]["properties"]
    assert props["node_type"]["enum"] == ["document", "reference"]
    assert "default" not in props["node_type"], (
        "omitted node_type KEEPS the kind — no default may imply one"
    )
    assert "node_type" not in tool["function"]["parameters"]["required"]


def test_agent_import_file_schema_vocabulary():
    from agent_tools.specs.media import IMPORT_FILE_TOOL

    params = IMPORT_FILE_TOOL["function"]["parameters"]
    props = params["properties"]
    assert "is_reference" not in props
    assert "document_id" not in props, "the host is parent_id now"
    assert props["node_type"]["enum"] == ["document", "reference"]
    assert "parent_id" in props


def test_mcp_projection_keeps_parent_id_required():
    """_entry_to_mcp rewrites `required` for some tools — create_document's
    parent_id requirement must SURVIVE that projection (an omitted placement
    must be refused at input validation, before dispatch)."""
    from agent_tools.registry import by_name, root_fill
    from mcp_gateway.schemas import _entry_to_mcp

    mcp_tool = _entry_to_mcp(by_name("create_document"), root_fill("", ""))
    assert "parent_id" in (mcp_tool.input_schema or {}).get("required", [])


@pytest.mark.asyncio
async def test_mcp_create_document_omitted_placement_errors(
    client, mcp_running, test_db, admin_user, project_with_doc,
):
    """Over MCP the omitted placement is caught BEFORE dispatch — the SDK
    validates the advertised `required` (parent_id) server-side, so the call
    never reaches the handler as a silent explicit-null root landing. The
    dispatch builder still preserves key-presence as defense in depth (a future
    schema that drops the required entry must surface the handler's 422, not a
    root create)."""
    pid, _, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "create_document",
                   "arguments": {"title": "T", "content": "x"}},
    }, headers={"Authorization": f"Bearer {tok}",
                "Accept": "application/json"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("result", {}).get("isError", False), data
    text = data["result"]["content"][0]["text"]
    assert "parent_id" in text, text


async def _agent_tool_definitions() -> dict[str, dict]:
    """Every agent tool DEFINITION, by name — including the ones agent_toolset()
    withholds when their gate is off.

    WHY: the subject of the description tests below is the wording a definition
    carries, which is a property of the entry itself and not of one deploy's
    config. await agent_toolset() serves SANDBOX_* only under SANDBOX_ENABLED, which
    config.py:561 derives from SANDBOX_SSH_HOST/SANDBOX_SSH_KEY_B64 — set in the
    dev container, unset on the CI runner. Reading the served list alone made
    these assertions pass or fail on the runner's env rather than on the code.
    Unioning the gated constants in removes the env axis instead of pinning it,
    because no assertion here is about what is currently SERVED.
    """
    from agent.tools import agent_toolset
    from agent_tools.specs.media import (
        SANDBOX_FETCH_REFERENCE_TOOL,
        SANDBOX_TOOL,
    )

    defs = {t["function"]["name"]: t for t in await agent_toolset()}
    for spec in (SANDBOX_TOOL, SANDBOX_FETCH_REFERENCE_TOOL):
        defs.setdefault(spec["function"]["name"], spec)
    return defs


async def test_no_agent_surface_description_names_attach_to():
    """The dead word must not linger in any served Pi description — a model
    generalizes vocabulary between tools, and one stale mention re-teaches the
    fork. attach_file (MCP-only, keeps attach_to deliberately) is out of scope
    here; this walks the agent surface."""
    for spec in (await _agent_tool_definitions()).values():
        blob = spec["function"]["description"] or ""
        for prop in (spec["function"]["parameters"].get("properties") or {}).values():
            blob += " " + (prop.get("description") or "")
        assert "attach_to" not in blob, (
            f"{spec['function']['name']} description still names attach_to"
        )


async def test_create_and_import_descriptions_do_not_teach_is_reference():
    """The tools whose ARGS carried is_reference (create_document via the
    removed attach_to derivation, import_file, and the sandbox tools that steer
    into import_file) must not teach the dead boolean as a knob."""
    surface = await _agent_tool_definitions()
    for name in ("create_document", "import_file",
                 "sandbox_bash", "sandbox_fetch_reference"):
        assert name in surface, f"{name} missing from the agent surface"
        blob = surface[name]["function"]["description"] or ""
        for prop in (surface[name]["function"]["parameters"]
                     .get("properties") or {}).values():
            blob += " " + (prop.get("description") or "")
        assert "is_reference" not in blob, (
            f"{name} description still teaches is_reference"
        )
