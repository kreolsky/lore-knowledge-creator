"""Contract tests for `sandbox_view_image` — the refused principals and the
image-result set the driver payload carries."""

import pytest
from test_sandbox_file_bridge import (  # noqa: F401 — fixtures resolve by name
    _hdr,
    _make_key,
    _member,
    sandbox_configured,
    sftp_seams,
)


# TEST-WHY: refused-principal — a commentator holding a valid internal key must not read workspace files back as images.
@pytest.mark.asyncio
async def test_view_image_rejects_commentator(client, test_db, project_with_doc, sftp_seams):  # noqa: F811
    import secrets

    pid, _, _ = project_with_doc
    guest = f"vi-guest-{secrets.token_hex(4)}"
    await _member(guest, pid, "commentator")
    token = await _make_key(guest, pid, internal=True)

    resp = await client.post("/api/tool/sandbox_view_image", json={
        "file_path": "chart.png",
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["read_calls"] == []


# TEST-WHY: refused-principal — an external MCP key, even the project owner's, never reaches the sandbox workspace.
@pytest.mark.asyncio
async def test_view_image_rejects_external_key(client, test_db, project_with_doc, sftp_seams):  # noqa: F811
    pid, _, uid = project_with_doc
    token = await _make_key(uid, pid, internal=False)

    resp = await client.post("/api/tool/sandbox_view_image", json={
        "file_path": "chart.png",
    }, headers=_hdr(token))
    assert resp.status_code == 403, resp.text
    assert sftp_seams["read_calls"] == []


# TEST-WHY: cross-process-contract — the plugin picks its image arm only from `image_result_tools`; the payload must carry exactly the registry's declared set.
def test_turn_payload_image_result_tools_match_registry():
    from agent_tools.registry import entries
    from driver.client import _derived_tool_sets

    declared = sorted(e.name for e in entries() if e.image_result)
    assert declared, "no tool declares image_result"
    assert _derived_tool_sets()["image_result_tools"] == declared
