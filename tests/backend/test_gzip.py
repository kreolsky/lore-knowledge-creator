"""GZip middleware: compresses large JSON, excludes text/event-stream (SSE).

httpx's test client auto-decompresses the body but preserves the Content-Encoding
response header, so we assert on the header (the on-the-wire signal) and on the
decoded JSON payload, never on raw bytes.
"""

import json

import pytest


@pytest.mark.asyncio
async def test_gzip_compresses_large_json(client, admin_user, project_with_doc):
    """A large JSON response carries Content-Encoding: gzip when the client accepts gzip."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Seed several references so the payload exceeds minimum_size (1024).
    for i in range(5):
        await client.post(
            "/api/references",
            json={
                "project_id": pid, "document_id": idx_id,
                "title": f"Ref {i} " + ("x" * 400), "media_type": "markdown",
                "content": "body " * 80,
            },
            cookies={"lore_session": token},
        )
    resp = await client.get(
        f"/api/references?project_id={pid}",
        cookies={"lore_session": token},
        headers={"Accept-Encoding": "gzip"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    # The auto-decompressed body is still valid JSON.
    assert isinstance(json.loads(resp.text), list)


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


@pytest.mark.asyncio
async def test_gzip_never_compresses_sse(app, client, admin_user):
    """The load-bearing guard: a text/event-stream response is NEVER gzipped, even
    when the client accepts gzip and the body far exceeds minimum_size.

    Why pinned (see main.py INVARIANT): Starlette's GZipResponder does not
    Z_SYNC_FLUSH per chunk, so a gzipped SSE would buffer until a full deflate block
    accumulated — coalescing the AI-chat token ticks and breaking the streaming UX.
    DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",) is what prevents that; this
    test guards the exclusion so a Starlette bump or middleware swap can't silently
    re-introduce buffering.

    A throwaway SSE route is mounted on the real app (so the actual middleware stack +
    minimum_size=1024 config is exercised), then removed so it doesn't leak to other
    tests. Auth is irrelevant to the middleware behaviour but the client sends the
    session cookie to match every other request shape.
    """
    from fastapi.responses import StreamingResponse

    _, token = admin_user

    async def sse_body():
        # > minimum_size (1024) so gzip WOULD engage for a compressible content type.
        yield b"data: " + (b"chunk " * 300) + b"\n\n"

    app.add_api_route(
        "/__test_sse_gzip",
        lambda: StreamingResponse(sse_body(), media_type="text/event-stream"),
        methods=["GET"],
    )
    try:
        resp = await client.get(
            "/__test_sse_gzip",
            cookies={"lore_session": token},
            headers={"Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        # The exclusion is the whole point: no Content-Encoding on an SSE response.
        assert resp.headers.get("content-encoding") != "gzip"
        # The body passed through uncompressed (still readable as SSE frames).
        assert resp.text.startswith("data:")
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__test_sse_gzip"
        ]
