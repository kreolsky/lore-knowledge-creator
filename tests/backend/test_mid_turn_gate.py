"""Tests for the mid-turn approval seam on the Tool-API surface.

# ARCH (plan collapse-agent-stack-onto-dsh-vocabulary step 7): the backend no
longer holds a confirm call — the ASK lives in the driver (dsh's user-approval
service). What this surface does instead:
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


async def _make_session(client, token: str, project_id: str, doc_id: str) -> str:
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": project_id, "document_id": doc_id, "model": "test-model"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _confirm_edit(doc_id: str) -> dict:
    return {
        "document_id": doc_id, "old_string": "beta", "new_string": "BETA",
        "apply": "confirm",
    }


class TestConfirmCell:
    @pytest.mark.asyncio
    async def test_mutating_confirm_refuses_with_the_ask_signal(
        self, bp, client, test_db, admin_user, project_with_doc,
    ):
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "Gate doc", "alpha beta")
        session_id = await _make_session(client, admin_token, pid, doc_id)

        resp = await client.post(
            "/api/tool/edit_document",
            json=_confirm_edit(doc_id),
            headers=_hdr(agent_token, "gate-call-1", session_id),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["code"] == "confirmation_required"
        assert "edit_document" in detail["detail"]

        # Refused, never applied: the document is unchanged.
        read = await client.post(
            "/api/tool/read_document",
            json={"document_id": doc_id},
            headers=_hdr(agent_token, "gate-call-ro-1", session_id),
        )
        assert "alpha beta" in read.json().get("content", "")

        # A read-only call with the same headers is never refused — it returns
        # immediately with no ask signal.
        ro = await client.post(
            "/api/tool/read_document",
            json={"document_id": doc_id},
            headers=_hdr(agent_token, "gate-call-ro-2", session_id),
        )
        assert ro.status_code == 200

    @pytest.mark.asyncio
    async def test_the_verdict_marker_applies_the_confirm_call(
        self, bp, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        """The driver's retry shape: the same confirm call plus the approval
        marker (dsh's user-approval service already asked) AND the driver
        secret that attests the marker applies through the ordinary auto path
        — the pair replaces the deleted verdict_approved flag."""
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "drv-s3cret")
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "Marker doc", "alpha beta")
        session_id = await _make_session(client, admin_token, pid, doc_id)

        resp = await client.post(
            "/api/tool/edit_document",
            json=_confirm_edit(doc_id),
            headers={
                **_hdr(agent_token, "gate-marker-1", session_id),
                "X-Agent-Verdict": "allowed-once",
                "X-Driver-Secret": "drv-s3cret",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("status") == "applied"

        read = await client.post(
            "/api/tool/read_document",
            json={"document_id": doc_id},
            headers=_hdr(agent_token, "gate-marker-ro", session_id),
        )
        assert "alpha BETA" in read.json().get("content", "")

    @pytest.mark.asyncio
    async def test_an_unattested_marker_still_refuses_the_confirm_call(
        self, bp, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        """The falsifier for the test above: the SAME call, the SAME marker,
        no driver secret.

        # INVARIANT(security): only OUR driver's marker converts a confirm
        # cell to auto. Why: the Tool-API is reachable by any agent-capable
        # key (external brains included) and the marker is a plain header —
        # unattested, it would let its holder self-approve every write the
        # confirm cell exists to stop, so the call refuses with the ordinary
        # ask signal and the document is left untouched.
        """
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "drv-s3cret")
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "Unattested", "alpha beta")
        session_id = await _make_session(client, admin_token, pid, doc_id)

        for secret in (None, "wrong-secret"):
            headers = {
                **_hdr(agent_token, f"gate-unattested-{secret}", session_id),
                "X-Agent-Verdict": "allowed-once",
            }
            if secret is not None:
                headers["X-Driver-Secret"] = secret
            resp = await client.post(
                "/api/tool/edit_document", json=_confirm_edit(doc_id), headers=headers,
            )
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"].get("code") == "confirmation_required"

        read = await client.post(
            "/api/tool/read_document",
            json={"document_id": doc_id},
            headers=_hdr(agent_token, "gate-unattested-ro", session_id),
        )
        assert read.json().get("content") == "alpha beta"

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

    @pytest.mark.asyncio
    async def test_a_full_caller_gets_the_ask_signal(
        self, bp, client, test_db, admin_user, project_with_doc,
    ):
        """The falsifier: the same call from a caller who CAN write gets the
        409 ask signal, so the test above measures the RBAC cell and not a
        route that refuses everyone."""
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "RBAC doc 2", "alpha beta")

        resp = await client.post(
            "/api/tool/edit_document",
            json=_confirm_edit(doc_id),
            headers=_hdr(agent_token, "rbac-call-2", "rbac-sess-2"),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].get("code") == "confirmation_required"


class TestApplyResolver:
    """The unified resolver's cells (pure — no I/O): the marker, the MCP
    force-auto override, and the system-doc confirm."""

    @staticmethod
    async def _resolve(ctx: dict, *, ui: str, system: bool):
        from routes.tool_api._common import _resolve_apply_or_force

        return await _resolve_apply_or_force(ctx, ui_preference=ui, is_system=system)

    @pytest.mark.asyncio
    async def test_the_marker_refuses_a_system_doc(self):
        # INVARIANT(security): a system-doc target refuses exactly like the MCP
        # force-auto path, even for a marker that CLEARED attestation — the
        # deleted verdict_approved flag was backend-held after a real verdict,
        # so is_system stays as defence in depth behind driver_attested.
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._resolve(
                {"verdict": "allowed-once"}, ui="confirm", system=True,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_the_marker_applies_a_non_system_doc(self):
        decision = await self._resolve(
            {"verdict": "allowed-once"}, ui="confirm", system=False,
        )
        assert decision.mode == "auto"
        assert decision.reason == "verdict_approved"

    @pytest.mark.asyncio
    async def test_mcp_force_auto_refuses_system_docs(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._resolve({"force_auto_apply": True}, ui="auto", system=True)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_mcp_force_auto_applies_non_system(self):
        decision = await self._resolve(
            {"force_auto_apply": True}, ui="confirm", system=False,
        )
        assert decision.mode == "auto"
        assert decision.reason == "mcp_force_auto"

    @pytest.mark.asyncio
    async def test_a_system_doc_without_approval_confirms(self):
        decision = await self._resolve({}, ui="auto", system=True)
        assert decision.mode == "confirm"
        assert decision.reason == "system_doc"

    @pytest.mark.asyncio
    async def test_the_ui_confirm_preference_confirms(self):
        decision = await self._resolve({}, ui="confirm", system=False)
        assert decision.mode == "confirm"
        assert decision.reason == "ui_confirm"

    @pytest.mark.asyncio
    async def test_an_auto_preference_applies(self):
        decision = await self._resolve({}, ui="auto", system=False)
        assert decision.mode == "auto"
        assert decision.reason == "opted_in"

    @pytest.mark.asyncio
    async def test_an_unknown_marker_value_is_ignored(self):
        decision = await self._resolve(
            {"verdict": "allowed-forever"}, ui="confirm", system=False,
        )
        assert decision.mode == "confirm"


class TestSystemDocRefusal:
    """A system-doc target reaches the confirm cell even in auto mode (the
    agent self-edit privilege path): the ask signal, never a silent apply."""

    @pytest.mark.asyncio
    async def test_system_doc_auto_without_approval_refuses(
        self, bp, client, test_db, admin_user, project_with_doc, monkeypatch,
    ):
        admin_uid, admin_token = admin_user
        pid, _, _ = project_with_doc
        agent_token = await _make_agent_key(test_db, admin_uid, pid)
        doc_id = await _make_doc(client, admin_token, pid, "System doc", "alpha beta")

        import db as db_mod

        real_fetch_one = db_mod.fetch_one

        async def system_flagged_fetch_one(table, _id):
            if table == "documents" and _id == doc_id:
                return {"project_id": pid, "is_system": True}
            return await real_fetch_one(table, _id)

        monkeypatch.setattr(db_mod, "fetch_one", system_flagged_fetch_one)

        resp = await client.post(
            "/api/tool/edit_document",
            json={
                "document_id": doc_id, "old_string": "beta", "new_string": "BETA",
                "apply": "auto",
            },
            headers=_hdr(agent_token, "sys-call-1", "sys-sess-1"),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].get("code") == "confirmation_required"


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
    async def test_the_marker_survives_a_matching_driver_secret(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "s3cret", raising=False)
        assert (await self._decode("allowed-once", "s3cret"))["verdict"] == "allowed-once"

    @pytest.mark.asyncio
    async def test_an_unattested_marker_is_dropped(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "s3cret", raising=False)
        # No secret at all — an agent key claiming its own approval.
        assert (await self._decode("allowed-once", None))["verdict"] is None
        assert (await self._decode("allowed-once", ""))["verdict"] is None
        # A wrong secret is the same refusal (compare_digest, not a prefix).
        assert (await self._decode("allowed-once", "s3cret-guess"))["verdict"] is None
        assert (await self._decode("allowed-once", "s3"))["verdict"] is None

    @pytest.mark.asyncio
    async def test_an_unconfigured_line_attests_nothing(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "", raising=False)
        assert (await self._decode("allowed-once", ""))["verdict"] is None
        assert (await self._decode("allowed-once", "anything"))["verdict"] is None

    @pytest.mark.asyncio
    async def test_the_other_correlation_headers_are_untouched(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "s3cret", raising=False)
        ctx = await self._decode(None, None)
        assert ctx == {
            "call_id": "call-1", "session_id": "sess-1",
            "message_id": "msg-1", "verdict": None,
        }
