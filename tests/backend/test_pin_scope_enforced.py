"""The pinned region is enforced from SERVER state, not from what the caller sends.

A pinned session (`chat_sessions.has_region`) constrains the agent to a fragment of
the pinned document. Before this file the only enforcement was containment of a
region the CALLER supplied: omitting `region` from the body left the edit
unconstrained, and the table tools — which mutate the same document's Y subtree but
carry no region — were never checked at all.

The gate lives in the generated Tool-API wrapper (agent_tools.routing), like the
mid-turn hold, so a third-party tool cannot omit itself from it. Region capability
is DERIVED from the request model (a `region` field), never a hand-kept list — the
next region-capable tool is covered by declaring the field.
"""

import hashlib
import secrets

import pytest


async def _make_agent_key(test_db, user_id, project_id):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"pin-key-{secrets.token_hex(4)}", {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "pin",
        "capabilities": ["agent"],
    })
    return token


async def _make_pinned_session(test_db, user_id, project_id, doc_id, *, has_region=True):
    from db import create_record

    sid = f"pin-sess-{secrets.token_hex(4)}"
    await create_record("chat_sessions", sid, {
        "user_id": user_id,
        "project_id": project_id,
        "document_id": doc_id,
        "has_region": has_region,
        "title": "pinned",
    })
    return sid


def _hdr(token, call_id, session_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Agent-Call-Id": call_id,
        "X-Agent-Session-Id": session_id,
    }


async def _make_doc(client, cookie, project_id, content):
    resp = await client.post(
        "/api/documents",
        json={"project_id": project_id, "title": "pinned.md", "content": content},
        cookies={"lore_session": cookie},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


class TestPinnedRegionIsServerEnforced:
    @pytest.mark.asyncio
    async def test_edit_omitting_the_region_is_refused_on_a_pinned_session(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """Fail-closed: the pin lives on the session row, so a body that simply
        leaves `region` out must NOT buy an unconstrained edit."""
        uid, cookie = admin_user
        pid, _, _ = project_with_doc
        token = await _make_agent_key(test_db, uid, pid)
        doc_id = await _make_doc(client, cookie, pid, "alpha beta gamma")
        sid = await _make_pinned_session(test_db, uid, pid, doc_id)

        resp = await client.post(
            "/api/tool/edit_document",
            json={
                "document_id": doc_id, "apply": "auto",
                "edits": [{"old_string": "gamma", "new_string": "GAMMA"}],
            },
            headers=_hdr(token, "pin-call-1", sid),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "region_required"

        doc = await client.get(
            f"/api/documents/{doc_id}", cookies={"lore_session": cookie},
        )
        assert doc.json()["content"] == "alpha beta gamma"

    @pytest.mark.asyncio
    async def test_table_cell_edit_is_refused_on_a_pinned_session(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """A table tool carries no region but mutates the pinned document's own Y
        subtree — outside the pinned TEXT region, so it is refused outright."""
        uid, cookie = admin_user
        pid, _, _ = project_with_doc
        token = await _make_agent_key(test_db, uid, pid)
        doc_id = await _make_doc(client, cookie, pid, "alpha beta gamma")
        sid = await _make_pinned_session(test_db, uid, pid, doc_id)

        resp = await client.post(
            "/api/tool/edit_table_cell",
            json={
                "document_id": doc_id, "apply": "auto",
                "edits": [{
                    "table_id": "t1", "row": 0, "column": "Name",
                    "old_value": "a", "new_value": "b",
                }],
            },
            headers=_hdr(token, "pin-call-2", sid),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "region_out_of_scope"

    @pytest.mark.asyncio
    async def test_unpinned_session_is_unconstrained(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The falsifier for the two above: with has_region false the SAME call
        applies. Without this the gate could be refusing everything."""
        uid, cookie = admin_user
        pid, _, _ = project_with_doc
        token = await _make_agent_key(test_db, uid, pid)
        doc_id = await _make_doc(client, cookie, pid, "alpha beta gamma")
        sid = await _make_pinned_session(test_db, uid, pid, doc_id, has_region=False)

        resp = await client.post(
            "/api/tool/edit_document",
            json={
                "document_id": doc_id, "apply": "auto",
                "edits": [{"old_string": "gamma", "new_string": "GAMMA"}],
            },
            headers=_hdr(token, "pin-call-3", sid),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "applied"

    @pytest.mark.asyncio
    async def test_a_call_against_another_document_is_untouched_by_the_pin(
        self, client, test_db, admin_user, project_with_doc,
    ):
        """The pin constrains the pinned DOCUMENT. A write to a different document
        is not what the user pinned, and stays allowed (this is the boundary the
        deleted proposal path drew too)."""
        uid, cookie = admin_user
        pid, _, _ = project_with_doc
        token = await _make_agent_key(test_db, uid, pid)
        pinned_id = await _make_doc(client, cookie, pid, "alpha beta gamma")
        other_id = await _make_doc(client, cookie, pid, "one two three")
        sid = await _make_pinned_session(test_db, uid, pid, pinned_id)

        resp = await client.post(
            "/api/tool/edit_document",
            json={
                "document_id": other_id, "apply": "auto",
                "edits": [{"old_string": "three", "new_string": "THREE"}],
            },
            headers=_hdr(token, "pin-call-4", sid),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "applied"


class TestRegionCapabilityIsDerived:
    def test_exactly_the_region_carrying_models_are_region_capable(self):
        """The gate reads capability off the request model, so the set cannot drift
        from the models the way a hand-kept list would. Asserted over the DERIVED
        registry, not a literal mirrored from the gate."""
        from agent_tools import registry
        from agent_tools.registry import _is_region_capable

        capable = {
            e.name for e in registry.tool_api_entries()
            if e.mutating and _is_region_capable(e)
        }
        assert capable == {"edit_document", "append_to_document"}

    def test_every_mutating_tool_is_classified(self):
        """No mutating tool falls through the gate unclassified: it is either
        region-capable (must carry the region) or out of scope (refused)."""
        from agent_tools import registry
        from agent_tools.registry import _is_region_capable

        for e in registry.tool_api_entries():
            if not e.mutating:
                continue
            assert isinstance(_is_region_capable(e), bool)


class TestForeignSessionIsNotAnOracle:
    @pytest.mark.asyncio
    async def test_foreign_pinned_session_does_not_differentiate_the_refusal(
        self, client, test_db, admin_user, regular_user, project_with_doc,
    ):
        """A FOREIGN session id must not change what the surface answers. The
        pin scope honors a pin only on the CALLER'S OWN session
        (pinned_session_doc_id), so a foreign caller tagging a REAL pinned
        session gets no pin-specific refusal — and with the ask living
        driver-side (step 7), every spelling of "a confirm call without
        approval" answers the SAME machine-readable ask signal, foreign
        session or nonexistent one alike: no oracle either way."""
        from db import create_record

        admin_uid, cookie = admin_user
        other_uid, _ = regular_user
        pid, _, _ = project_with_doc
        await create_record("project_members", "test-pm-pin-oracle", {
            "project_id": pid, "user_id": other_uid, "access_level": "full",
        })
        doc_id = await _make_doc(client, cookie, pid, "alpha beta gamma")
        sid = await _make_pinned_session(test_db, admin_uid, pid, doc_id)
        other_tok = await _make_agent_key(test_db, other_uid, pid)

        resp = await client.post(
            "/api/tool/edit_document",
            json={
                "document_id": doc_id,
                "edits": [{"old_string": "gamma", "new_string": "GAMMA"}],
                "apply": "confirm",
            },
            headers=_hdr(other_tok, "pin-oracle-1", sid),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].get("code") == "confirmation_required"

        # The nonexistent-session spelling answers identically — no oracle.
        resp2 = await client.post(
            "/api/tool/edit_document",
            json={
                "document_id": doc_id,
                "edits": [{"old_string": "gamma", "new_string": "GAMMA"}],
                "apply": "confirm",
            },
            headers=_hdr(other_tok, "pin-oracle-2", "no-such-session"),
        )
        assert resp2.status_code == 409, resp2.text
        assert resp2.json()["detail"].get("code") == "confirmation_required"
