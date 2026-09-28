"""B1 — central uncaught-500 handler + X-Request-ID middleware.

Verifies the real handler/middleware (imported from main.py) across the THREE
raise surfaces FastAPI's ExceptionMiddleware covers unevenly: (а) a route
handler, (б) a dependency, (в) a middleware. Each must return a logged 500 with
body {"detail": "Internal server error"} (the contract client.ts:65 depends on)
and carry an X-Request-ID header. HTTPException handling is left to FastAPI and
must NOT be intercepted.

The app below mirrors main.py's prod middleware ordering (request_id INNERMOST,
so route/dependency exceptions reach it before the outer BaseHTTPMiddlewares).
KNOWN GAP: the negative "request_id-outermost → BaseExceptionGroup escape"
variant is intentionally omitted — anyio only wraps a propagating exception in
a BaseExceptionGroup when a CancelledError is mixed in (a non-deterministic
edge case); a plain RuntimeError is re-raised directly and `except Exception`
catches it, so a negative test would be flaky. The fidelity gain comes from
mirroring prod ordering end-to-end (positive variant only).
"""

import logging

import pytest
from fastapi import Depends, FastAPI, HTTPException
from starlette.testclient import TestClient

from main import REQUEST_ID_HEADER, request_id_middleware, uncaught_exception_handler


def _build_app() -> FastAPI:
    app = FastAPI()

    # Register request_id FIRST → innermost user middleware (mirrors main.py prod
    # ordering, where request_id is added before CORS/CSRF/json_body). WHY innermost:
    # a route/dependency exception is re-raised by the inner ExceptionMiddleware and
    # reaches request_id BEFORE traversing the outer BaseHTTPMiddlewares, where anyio
    # could wrap it in a BaseExceptionGroup that ServerErrorMiddleware's `except Exception`
    # does not reliably catch. See ARCH block in main.py.
    app.middleware("http")(request_id_middleware)

    # A passthrough BaseHTTPMiddleware registered AFTER request_id → OUTER to it
    # (mirrors csrf/json_body sitting outside request_id in prod). Non-raising; its
    # presence is what makes the innermost placement load-bearing.
    @app.middleware("http")
    async def outer_passthrough(request, call_next):
        return await call_next(request)

    # An outer middleware that RAISES on a specific path (mirrors a csrf/json_body
    # raise). Sits OUTSIDE request_id, so its exception CANNOT reach request_id — it
    # must be caught by the secondary @app.exception_handler(Exception) net (Tier 2).
    @app.middleware("http")
    async def outer_raising(request, call_next):
        if request.url.path == "/mid-raise":
            raise RuntimeError("middleware boom")
        return await call_next(request)

    # Secondary net (Tier 2) — the real handler from main.py.
    app.add_exception_handler(Exception, uncaught_exception_handler)

    @app.get("/route-raise")
    async def route_raise():
        raise RuntimeError("route boom")

    @app.get("/dep-raise")
    async def dep_raise(_x: None = Depends(lambda: (_ for _ in ()).throw(RuntimeError("dep boom")))):
        return {"ok": True}

    @app.get("/http-exc")
    async def http_exc():
        raise HTTPException(status_code=404, detail="not found")

    return app


@pytest.fixture
def app():
    return _build_app()


@pytest.mark.parametrize("path", ["/route-raise", "/dep-raise", "/mid-raise"])
def test_uncaught_500_logged_with_request_id(app, path, caplog):
    # WHY TestClient + raise_server_exceptions=False: the Tier-2 path (/mid-raise)
    # raises OUTSIDE request_id, so the exception reaches ServerErrorMiddleware,
    # which calls the handler, sends the 500 response, and then ALWAYS re-raises
    # the exception ("allows test clients to optionally raise the error"). Plain
    # httpx.ASGITransport propagates that re-raise and discards the sent response;
    # Starlette's TestClient with raise_server_exceptions=False captures the
    # response sent before the re-raise and returns it. The Tier-1 paths
    # (/route-raise, /dep-raise) never re-raise (request_id catches and returns),
    # so the flag is a no-op there.
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="main"):
        resp = client.get(path)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}
    assert REQUEST_ID_HEADER in resp.headers
    assert resp.headers[REQUEST_ID_HEADER]
    assert any("Unhandled exception" in r.message for r in caplog.records)


def test_inbound_request_id_is_honored(app):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/route-raise", headers={REQUEST_ID_HEADER: "trace-123"})
    assert resp.status_code == 500
    assert resp.headers[REQUEST_ID_HEADER] == "trace-123"


def test_http_exception_not_intercepted(app):
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/http-exc")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "not found"}
