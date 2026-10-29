"""GZip middleware: compresses large JSON, excludes text/event-stream (SSE).

httpx's test client auto-decompresses the body but preserves the Content-Encoding
response header, so we assert on the header (the on-the-wire signal) and on the
decoded JSON payload, never on raw bytes.
"""


import pytest


@pytest.mark.asyncio
async def test_gzip_disabled_without_accept_encoding(client, admin_user, project_with_doc):
    """No Accept-Encoding → no compression (passthrough)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "R", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/references?project_id={pid}",
        cookies={"lore_session": token},
        headers={"Accept-Encoding": "identity"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") != "gzip"
