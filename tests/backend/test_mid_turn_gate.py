"""Tests for the mid-turn approval seam on the Tool-API surface.

# ARCH: the backend does not hold a confirm call — the ASK lives in the
driver (dsh's user-approval service). What this surface does instead:
  - a mutating call whose apply-mode resolves to `confirm` REFUSES with
    409 {code: confirmation_required} — the machine-readable ask signal the
    Lore driver keys its approval retry on;
  - the driver's retry carries X-Agent-Verdict: allowed-once (the marker), the
    replacement for the deleted backend hold's verdict_approved flag — the
    approved call applies through the ordinary auto path;
  - a caller who cannot write the target is refused at dispatch (403), never
    shown an ask — the refusal that used to precede the hold now precedes the
    ask signal.
"""

import hashlib
import secrets

import pytest
import pytest_asyncio
from backplane import get_backplane, reset_backplane


@pytest_asyncio.fixture
async def bp():
    """The singleton RedisBackplane, reset and closed between tests."""
    reset_backplane()
    instance = get_backplane()
    yield instance
    try:
        await instance.close()
    except Exception:
        pass
    reset_backplane()


async def _make_agent_key(test_db, user_id, project_id, label="agent"):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"agent-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",
        "token_hash": token_hash,
        "label": label,
        "capabilities": ["agent"],
    })
    return token


def _hdr(token: str, call_id: str, session_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Agent-Call-Id": call_id,
        "X-Agent-Session-Id": session_id,
    }


async def _make_doc(client, token: str, project_id: str, title: str, content: str) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


def _confirm_edit(doc_id: str) -> dict:
    return {
        "document_id": doc_id, "old_string": "beta", "new_string": "BETA",
        "apply": "confirm",
    }


class TestConfirmCell:


    @pytest.mark.asyncio
    async def test_a_non_full_caller_is_refused_at_dispatch_never_shown_an_ask(
        self, bp, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        """A caller who cannot write the target is turned away at dispatch
        (403 from gate_mutation_target), never handed the ask signal — the card
        invites the user to approve a call that is going to 403 anyway."""
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "RBAC doc", "alpha beta")

        import access as access_mod

        async def commentator(_doc_id, _user):
            return "commentator"

        monkeypatch.setattr(access_mod, "get_document_access", commentator)

        resp = await client.post(
            "/api/tool/edit_document",
            json=_confirm_edit(doc_id),
            headers=_hdr(agent_token, "rbac-call-1", "rbac-sess-1"),
        )
        assert resp.status_code == 403, resp.text


class TestApplyResolver:
    """The unified resolver's cells (pure — no I/O): the marker, the MCP
    force-auto override, and the system-doc confirm."""

    @staticmethod
    async def _resolve(ctx: dict, *, ui: str, system: bool):
        from routes.tool_api._common import _resolve_apply_or_force

        return await _resolve_apply_or_force(ctx, ui_preference=ui, is_system=system)


    @pytest.mark.asyncio
    async def test_mcp_force_auto_refuses_system_docs(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._resolve({"force_auto_apply": True}, ui="auto", system=True)
        assert exc.value.status_code == 403


class TestSystemDocRefusal:
    """A system-doc target reaches the confirm cell even in auto mode (the
    agent self-edit privilege path): the ask signal, never a silent apply."""


    @pytest.mark.asyncio
    async def test_system_doc_applies_after_the_attested_approval(
        self, bp, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        """The self-edit path end to end (the Knowledge-doc case): the call
        asks (409), an UNATTESTED marker still asks, and the driver's retry —
        the marker plus the driver secret, sent only after the user allowed —
        applies the edit to the system doc."""
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "drv-s3cret", raising=False)
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "Knowledge", "alpha beta")
        await test_db.query(
            "UPDATE type::record('documents', $id) SET is_system = true", {"id": doc_id})
        body = {"document_id": doc_id, "old_string": "beta", "new_string": "BETA"}
        hdr = _hdr(agent_token, "sys-call-2", "sys-sess-2")

        resp = await client.post("/api/tool/edit_document", json=body, headers=hdr)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].get("code") == "confirmation_required"

        resp = await client.post("/api/tool/edit_document", json=body, headers={
            **hdr, "X-Agent-Verdict": "allowed-once"})
        assert resp.status_code == 409, resp.text

        resp = await client.post("/api/tool/edit_document", json=body, headers={
            **hdr, "X-Agent-Verdict": "allowed-once", "X-Driver-Secret": "drv-s3cret"})
        assert resp.status_code == 200, resp.text
        doc = await client.get(
            f"/api/documents/{doc_id}", cookies={"lore_session": admin_token})
        assert "alpha BETA" in doc.json()["content"]


    # TEST-WHY: refused-principal — a Rules child is the agent's own instructions though it carries no is_system flag: an auto edit is refused with the every-call ask, an MCP force-apply is refused 403, while a Memory child keeps its own auto path.
    @pytest.mark.asyncio
    async def test_agent_config_is_a_system_doc_by_ancestry_except_memory(self, bp, client, test_db, admin_user, project_with_doc):  # noqa: E501
        from agent_config import ensure_agent_system_docs
        from fastapi import HTTPException
        from routes.tool_api._common import _is_system_target, _resolve_apply_or_force

        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        roles = await ensure_agent_system_docs(pid)

        async def child(parent_id: str, title: str) -> str:
            resp = await client.post("/api/documents", json={
                "project_id": pid, "title": title, "content": "alpha beta",
                "parent_id": parent_id,
            }, cookies={"lore_session": admin_token})
            assert resp.status_code == 200, resp.text
            return resp.json()["document_id"]

        rule_id = await child(roles["rules_folder"], "A rule")
        memory_id = await child(roles["memory_folder"], "A memory note")
        auto = {"old_string": "beta", "new_string": "BETA", "apply": "auto"}

        resp = await client.post(
            "/api/tool/edit_document", json={"document_id": rule_id, **auto},
            headers=_hdr(agent_token, "anc-call-1", "anc-sess-1"),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["ask_every_time"] is True

        assert await _is_system_target(rule_id, pid) is True
        with pytest.raises(HTTPException) as exc:
            await _resolve_apply_or_force(
                {"force_auto_apply": True}, ui_preference="auto",
                is_system=await _is_system_target(rule_id, pid),
            )
        assert exc.value.status_code == 403
        assert await _is_system_target(memory_id, pid) is False


    # TEST-WHY: refused-principal — facts have one door: an agent create_document under the Memory folder or under a fact, and a rename of a fact, are refused 403 even in auto mode; and a live page under a TRASHED Rules folder still counts as agent config.
    @pytest.mark.asyncio
    async def test_memory_one_door_and_trashed_ancestor(self, bp, client, test_db, admin_user, project_with_doc):  # noqa: E501
        from agent_config import ensure_agent_system_docs
        from share_guard import is_agent_instruction_doc

        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        roles = await ensure_agent_system_docs(pid)

        async def child(parent_id: str, title: str) -> str:
            resp = await client.post("/api/documents", json={
                "project_id": pid, "title": title, "content": "alpha",
                "parent_id": parent_id,
            }, cookies={"lore_session": admin_token})
            assert resp.status_code == 200, resp.text
            return resp.json()["document_id"]

        fact_id = await child(roles["memory_folder"], "Fact: gist")
        await test_db.query(
            "UPDATE type::record('documents', $id) SET is_memory = true", {"id": fact_id})
        for i, parent in enumerate((roles["memory_folder"], fact_id)):
            resp = await client.post("/api/tool/create_document", json={
                "title": "sneak", "content": "x", "parent_id": parent, "apply": "auto",
            }, headers=_hdr(agent_token, f"mem-call-{i}", "mem-sess-1"))
            assert resp.status_code == 403, resp.text
        resp = await client.post("/api/tool/rename_document", json={
            "document_id": fact_id, "title": "Renamed", "apply": "auto",
        }, headers=_hdr(agent_token, "mem-call-r", "mem-sess-1"))
        assert resp.status_code == 403, resp.text

        folder = await child(roles["rules_folder"], "Folder")
        page = await child(folder, "Page")
        await test_db.query(
            "UPDATE type::record('documents', $id) SET deleted_at = time::now()", {"id": folder})
        assert await is_agent_instruction_doc(page, pid) is True


class TestVerdictAttestation:
    """The marker is only READ on a driver-attested request.

    # INVARIANT(security): X-Agent-Verdict is a plain header on a surface any
    # agent-capable key can reach (external brains included) and it converts a
    # confirm cell to auto. Only OUR driver holds the driver secret, so an
    # unattested marker is dropped and the call falls back to the 409 ask.
    """

    @staticmethod
    async def _decode(verdict, driver_secret):
        from agent.context import _decode_correlation

        return await _decode_correlation(
            "call-1", "sess-1", "msg-1", verdict, driver_secret,
        )


    @pytest.mark.asyncio
    async def test_an_unconfigured_line_attests_nothing(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "", raising=False)
        assert (await self._decode("allowed-once", ""))["verdict"] is None
        assert (await self._decode("allowed-once", "anything"))["verdict"] is None
