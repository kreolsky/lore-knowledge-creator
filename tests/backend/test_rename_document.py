"""rename_document — the SOLE rename core (SYSTEM: documents).

Contract (plan rename-core-one-path): one core in documents.service serves both
REST PATCH surfaces. Doc rename emits document_renamed + content_flushed; ref
rename emits reference_renamed + content_flushed(is_reference=True); a
same-title PATCH is a no-op (no UPDATE, no events); whitespace-only title 400s;
unknown id 404s.
"""

import hashlib
import secrets

import pytest
import pytest_asyncio
from backplane import get_backplane, reset_backplane


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
