"""rename_document — the SOLE rename core (SYSTEM: documents).

Contract (plan rename-core-one-path): one core in documents.service serves both
REST PATCH surfaces. Doc rename emits document_renamed + content_flushed; ref
rename emits reference_renamed + content_flushed(is_reference=True); a
same-title PATCH is a no-op (no UPDATE, no events); whitespace-only title 400s;
unknown id 404s.
"""

import asyncio
import hashlib
import secrets
from contextlib import contextmanager

import pytest
import pytest_asyncio
from backplane import get_backplane, reset_backplane
from documents.service import rename_document
from fastapi import HTTPException


@contextmanager
def _catch_events(*types):
    """Register one recorder per event type; yields {type: [kwargs, ...]}."""
    from event_bus import off, on

    events: dict[str, list[dict]] = {t: [] for t in types}

    def _make(t):
        async def _rec(**kwargs):
            events[t].append(kwargs)
        return _rec

    handlers = {t: _make(t) for t in types}
    for t, h in handlers.items():
        on(t, h)
    try:
        yield events
    finally:
        for t, h in handlers.items():
            off(t, h)


async def _drain_bus():
    # emit() spawns subscribers as fire-and-forget tasks — two loop turns let
    # them run (same pattern as test_project_ws / test_reference_archive).
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _make_doc(client, token, pid: str, title: str, parent_id=None) -> str:
    body: dict = {"project_id": pid, "title": title}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=body,
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_ref(client, token, pid: str, host_id: str, title: str) -> str:
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": host_id, "title": title,
              "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


class TestRenameDocumentCore:
    """Direct calls against the core — event routing is read off the row."""

    @pytest.mark.asyncio
    async def test_doc_rename_emits_renamed_and_flush(
        self, client, admin_user, project_with_doc,
    ):
        pid, idx_id, _ = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Core Doc")

        with _catch_events(
            "document_renamed", "reference_renamed", "content_flushed",
        ) as ev:
            result = await rename_document(document_id=doc_id, title="Renamed Core")
            await _drain_bus()

        assert result == {
            "status": "renamed", "document_id": doc_id, "title": "Renamed Core",
            "is_reference": False, "unchanged": False,
        }
        assert ev["document_renamed"] == [
            {"project_id": pid, "document_id": doc_id, "title": "Renamed Core"},
        ]
        assert ev["reference_renamed"] == []
        assert ev["content_flushed"] == [
            {"entity_type": "doc", "entity_id": doc_id, "project_id": pid},
        ]

    @pytest.mark.asyncio
    async def test_ref_rename_emits_reference_renamed_and_flush(
        self, client, admin_user, project_with_doc,
    ):
        pid, idx_id, _ = project_with_doc
        _, token = admin_user
        ref_id = await _make_ref(client, token, pid, idx_id, "Core Ref")

        with _catch_events(
            "document_renamed", "reference_renamed", "content_flushed",
        ) as ev:
            result = await rename_document(document_id=ref_id, title="Renamed Ref")
            await _drain_bus()

        assert result == {
            "status": "renamed", "document_id": ref_id, "title": "Renamed Ref",
            "is_reference": True, "unchanged": False,
        }
        assert ev["document_renamed"] == []
        assert ev["reference_renamed"] == [
            {"project_id": pid, "reference_id": ref_id, "title": "Renamed Ref"},
        ]
        assert ev["content_flushed"] == [
            {"entity_type": "doc", "entity_id": ref_id, "project_id": pid,
             "is_reference": True},
        ]

    @pytest.mark.asyncio
    async def test_same_title_rename_is_noop(
        self, client, admin_user, project_with_doc,
    ):
        """Delta (b): a same-title PATCH emits nothing — today it emits a
        rename plus a content_flushed whose re-embed is already a hash no-op."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Same Title")

        with _catch_events(
            "document_renamed", "reference_renamed", "content_flushed",
        ) as ev:
            result = await rename_document(document_id=doc_id, title="Same Title")
            await _drain_bus()

        assert result["unchanged"] is True
        assert result["title"] == "Same Title"
        assert ev["document_renamed"] == []
        assert ev["reference_renamed"] == []
        assert ev["content_flushed"] == []

    @pytest.mark.asyncio
    async def test_title_stripped_before_compare_and_write(
        self, client, admin_user, project_with_doc, test_db,
    ):
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Padded")

        # Padded input equal after strip → no-op, not a whitespace rename.
        result = await rename_document(document_id=doc_id, title="  Padded  ")
        assert result["unchanged"] is True

        result = await rename_document(document_id=doc_id, title="  New Padded  ")
        assert result["title"] == "New Padded"
        rows = await test_db.query(
            "SELECT title FROM type::record('documents', $id)", {"id": doc_id},
        )
        assert rows[0]["title"] == "New Padded"

    @pytest.mark.asyncio
    async def test_empty_or_whitespace_title_400s(
        self, client, admin_user, project_with_doc,
    ):
        """Delta (a): whitespace-only title 400s — today it is written as the
        title."""
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Keep")
        for bad in ("", "   ", "\t\n"):
            with pytest.raises(HTTPException) as ei:
                await rename_document(document_id=doc_id, title=bad)
            assert ei.value.status_code == 400
            assert ei.value.detail == "title is required"

    @pytest.mark.asyncio
    async def test_missing_id_404s(self):
        with pytest.raises(HTTPException) as ei:
            await rename_document(document_id="no-such-doc", title="x")
        assert ei.value.status_code == 404
        assert ei.value.detail == "Document not found"


class TestRenameRestDeltas:
    """The two REST PATCH surfaces route through the core (deltas visible)."""

    @pytest.mark.asyncio
    async def test_doc_patch_whitespace_title_400s(
        self, client, admin_user, project_with_doc,
    ):
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Keep")
        resp = await client.patch(
            f"/api/documents/{doc_id}", json={"title": "   "},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "title is required"

    @pytest.mark.asyncio
    async def test_ref_patch_whitespace_title_400s(
        self, client, admin_user, project_with_doc,
    ):
        pid, idx_id, _ = project_with_doc
        _, token = admin_user
        ref_id = await _make_ref(client, token, pid, idx_id, "Keep Ref")
        resp = await client.patch(
            f"/api/references/{ref_id}", json={"title": " "},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "title is required"

    @pytest.mark.asyncio
    async def test_doc_patch_same_title_emits_nothing(
        self, client, admin_user, project_with_doc,
    ):
        pid, _, _ = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Same Same")

        with _catch_events(
            "document_renamed", "reference_renamed", "content_flushed",
        ) as ev:
            resp = await client.patch(
                f"/api/documents/{doc_id}", json={"title": "Same Same"},
                cookies={"lore_session": token},
            )
            await _drain_bus()

        assert resp.status_code == 200
        assert ev["document_renamed"] == []
        assert ev["content_flushed"] == []


# ════════════════════════════════════════════════════════════════════════════
# rename_document — the agent TOOL surface (plan rename-document-tool)
# ════════════════════════════════════════════════════════════════════════════


async def _make_agent_key(
    test_db, user_id, project_id, *, auto_apply=False, document_id="",
):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"rn-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": document_id,
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def bp():
    """The singleton RedisBackplane, reset and closed between tests (same as
    test_mid_turn_gate — the hold test needs the real verdict pub/sub)."""
    reset_backplane()
    instance = get_backplane()
    yield instance
    try:
        await instance.close()
    except Exception:
        pass
    reset_backplane()


class TestRenameDocumentTool:
    """tool_rename_document — move's handler shape over the core: gate →
    apply-resolution → hold-on-confirm → documents.service.rename_document.
    Adds no rename logic; the event routing stays the core's (read off the row).
    """

    @pytest.mark.asyncio
    async def test_auto_path_applies_doc_and_emits(
        self, client, test_db, admin_user, project_with_doc,
    ):
        pid, _, admin_uid = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Tool Doc")
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        with _catch_events("document_renamed", "reference_renamed") as ev:
            resp = await client.post("/api/tool/rename_document", json={
                "document_id": doc_id, "title": "Tool Renamed", "apply": "auto",
            }, headers=_hdr(agent_tok))
            await _drain_bus()

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "applied", "document_id": doc_id, "title": "Tool Renamed",
            "is_reference": False, "unchanged": False,
        }
        assert ev["document_renamed"] == [
            {"project_id": pid, "document_id": doc_id, "title": "Tool Renamed"},
        ]
        assert ev["reference_renamed"] == []

    @pytest.mark.asyncio
    async def test_reference_rename_routes_reference_event(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """Doc-vs-reference is invisible in the surface: no is_reference /
        node_type param — the core routes the event off the row."""
        pid, idx_id, admin_uid = project_with_doc
        _, token = admin_user
        ref_id = await _make_ref(client, token, pid, idx_id, "Tool Ref")
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        with _catch_events("document_renamed", "reference_renamed") as ev:
            resp = await client.post("/api/tool/rename_document", json={
                "document_id": ref_id, "title": "Tool Ref Renamed", "apply": "auto",
            }, headers=_hdr(agent_tok))
            await _drain_bus()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "applied"
        assert body["is_reference"] is True
        assert body["title"] == "Tool Ref Renamed"
        assert ev["document_renamed"] == []
        assert ev["reference_renamed"] == [
            {"project_id": pid, "reference_id": ref_id, "title": "Tool Ref Renamed"},
        ]

    @pytest.mark.asyncio
    async def test_same_title_returns_unchanged_no_events(
        self, client, test_db, admin_user, project_with_doc,
    ):
        pid, _, admin_uid = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Already Named")
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        with _catch_events("document_renamed", "reference_renamed") as ev:
            resp = await client.post("/api/tool/rename_document", json={
                "document_id": doc_id, "title": "Already Named", "apply": "auto",
            }, headers=_hdr(agent_tok))
            await _drain_bus()

        assert resp.status_code == 200, resp.text
        assert resp.json()["unchanged"] is True
        assert resp.json()["status"] == "applied"
        assert ev["document_renamed"] == []
        assert ev["reference_renamed"] == []

    @pytest.mark.asyncio
    async def test_is_system_403s_before_apply_resolution(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The system 403 fires BEFORE apply resolution: a confirm call with no
        correlation headers would 409 (unholdable) if resolution ran first —
        asserting 403 pins the gate's order, parity with the move gate."""
        pid, _, admin_uid = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Sys")
        await test_db.query(
            "UPDATE type::record('documents', $id) SET is_system = true",
            {"id": doc_id},
        )
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        resp = await client.post("/api/tool/rename_document", json={
            "document_id": doc_id, "title": "Nope", "apply": "confirm",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "System documents cannot be renamed"

    @pytest.mark.asyncio
    async def test_scoped_key_out_of_subtree_403s(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """A subtree-scoped key renames inside its sandbox and is walled
        outside it (the distinct scope 403, not a uniform 404)."""
        pid, _, admin_uid = project_with_doc
        _, token = admin_user
        root = await _make_doc(client, token, pid, "Scope Root")
        inside = await _make_doc(client, token, pid, "Inside", parent_id=root)
        outside = await _make_doc(client, token, pid, "Outside")
        agent_tok = await _make_agent_key(
            test_db, admin_uid, pid, auto_apply=True, document_id=root,
        )

        ok = await client.post("/api/tool/rename_document", json={
            "document_id": inside, "title": "Inside Renamed", "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert ok.status_code == 200, ok.text

        resp = await client.post("/api/tool/rename_document", json={
            "document_id": outside, "title": "Nope", "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_empty_title_400s_via_core(
        self, client, test_db, admin_user, project_with_doc,
    ):
        pid, _, admin_uid = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Keep Title")
        agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)

        resp = await client.post("/api/tool/rename_document", json={
            "document_id": doc_id, "title": "   ", "apply": "auto",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "title is required"

    @pytest.mark.asyncio
    async def test_confirm_default_refuses_with_ask_signal_then_marker_applies(
        self, bp, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        """apply defaults to confirm: without the driver's approval marker the
        call refuses with the machine-readable ask signal (409
        confirmation_required) — the Lore driver then asks through dsh's
        approval service and retries with X-Agent-Verdict PLUS the driver
        secret that attests it, which applies (the attested pair replaces the
        deleted backend hold's verdict_approved bypass)."""
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "drv-s3cret")
        pid, _, admin_uid = project_with_doc
        _, token = admin_user
        doc_id = await _make_doc(client, token, pid, "Hold Me")
        agent_tok = await _make_agent_key(test_db, admin_uid, pid)

        resp = await client.post("/api/tool/rename_document", json={
            "document_id": doc_id, "title": "Held Renamed", "apply": "confirm",
        }, headers=_hdr(agent_tok))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "confirmation_required"

        # An UNATTESTED marker changes nothing — the same ask signal. The
        # header alone is any agent key's claim about itself.
        resp_bare = await client.post("/api/tool/rename_document", json={
            "document_id": doc_id, "title": "Bare Renamed", "apply": "confirm",
        }, headers={**_hdr(agent_tok), "X-Agent-Verdict": "allowed-once"})
        assert resp_bare.status_code == 409, resp_bare.text

        # The driver's retry shape: the same call, the marker, the secret.
        resp2 = await client.post("/api/tool/rename_document", json={
            "document_id": doc_id, "title": "Marker Renamed", "apply": "confirm",
        }, headers={
            **_hdr(agent_tok),
            "X-Agent-Verdict": "allowed-once",
            "X-Driver-Secret": "drv-s3cret",
        })
        assert resp2.status_code == 200, resp2.text
        assert resp2.json().get("status") == "applied"


class TestRenameDocumentSurface:
    """The tool's declaration surface: serving order (Pi = declaration order,
    rename after move_document), MCP membership (appended — _TOOL_LIST_ORDER
    deliberately not edited), mutating + holdable derivation, and the
    MCP description override (proposal-free wording)."""

    async def test_served_after_move_document_on_pi(self):
        from agent.tools import AGENT_TOOLS, agent_toolset

        names = [t["function"]["name"] for t in AGENT_TOOLS]
        assert names.index("rename_document") == names.index("move_document") + 1
        full = [t["function"]["name"] for t in await agent_toolset()]
        assert full.index("rename_document") == full.index("move_document") + 1

    def test_served_on_mcp(self):
        from mcp_gateway.schemas import build_tool_list

        names = [t.name for t in build_tool_list()]
        assert "rename_document" in names

    def test_mutating_and_holdable_derive_from_declaration(self):
        from agent.tools import HOLDABLE_TOOLS, MUTATING_TOOLS

        assert "rename_document" in MUTATING_TOOLS
        assert "rename_document" in HOLDABLE_TOOLS

    def test_mcp_description_is_the_proposal_free_override(self):
        from agent_tools.registry import by_name

        entry = by_name("rename_document")
        assert entry.mcp_description
        assert entry.mcp_description.startswith("Rename any node")
        assert entry.mcp_description.rstrip().endswith(
            "Read-write key: applies immediately.",
        )
        assert "In confirmation mode" not in entry.mcp_description

    def test_agent_spec_description_carries_the_rename_contract(self):
        from agent.tools import AGENT_TOOLS

        spec = next(
            t for t in AGENT_TOOLS if t["function"]["name"] == "rename_document"
        )
        desc = spec["function"]["description"]
        assert desc.startswith("Propose renaming a node")
        assert "attached file node" in desc
        assert "stripped" in desc and "non-empty" in desc
        assert "never changes `document_id`" in desc
        assert "NOT reversible" in desc
        assert "In confirmation mode" in desc
        title_prop = spec["function"]["parameters"]["properties"]["title"]
        assert title_prop["description"] == (
            "The new title. Titles need not be unique — ids are the address."
        )
        assert set(spec["function"]["parameters"]["required"]) == {
            "document_id", "title",
        }
