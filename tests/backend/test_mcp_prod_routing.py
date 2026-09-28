"""MCP prod-routing guard — pins that /mcp is proxied to the backend in nginx.conf.

# WHY: the MCP gateway is mounted on the backend at /mcp (main.py), but in prod
# only the frontend (nginx) container publishes ports; the backend has none. If
# frontend/nginx.conf lacks a /mcp block, POST /mcp falls through to the SPA
# `try_files ... /index.html` and returns HTML 200 — the gateway is unreachable
# and every tool call silently fails. The in-process MCP tests (test_mcp_gateway_*)
# run the session manager directly in the ASGI client and never traverse nginx, so
# this regression is invisible to the suite. This static config assertion catches
# the exact regression (block deleted) cheaply, without Docker-in-CI.

# Precedent: backend tests cross-check frontend files (test_event_wiring.py reads
# /frontend/src/events, mounted read-only in docker-compose.yml). nginx.conf is
# mounted the same way (./frontend/nginx.conf:/frontend/nginx.conf:ro).
"""

import pathlib
import re

import pytest

_NGINX_CONF_CANDIDATES = (
    "/frontend/nginx.conf",        # compose mount (local dev) / docker cp (CI, canonical path)
    "frontend/nginx.conf",         # run from repo root on host
)


def _read_nginx_conf() -> str:
    for c in _NGINX_CONF_CANDIDATES:
        if pathlib.Path(c).exists():
            return pathlib.Path(c).read_text()
    pytest.skip(
        "frontend/nginx.conf not reachable. Locally: ensure the "
        "'./frontend/nginx.conf:/frontend/nginx.conf:ro' volume (docker-compose.yml "
        "backend service). In CI: ensure ci.yml docker-cp's it to /frontend/nginx.conf "
        "(the canonical path)."
    )


def _location_block(text: str, selector: str) -> str | None:
    """Return the text of the `location <selector> { ... }` block, or None.

    Matches the longest run from the `location <selector>` opening line to the
    matching closing brace at column 0. Order-independent (nginx uses longest-
    prefix match, not source order).
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        # opening line: `location /mcp {`
        if stripped.startswith("location") and stripped.split()[1:2] == [selector]:
            depth = 0
            started = False
            block: list[str] = []
            for j in range(i, len(lines)):
                blk_line = lines[j]
                block.append(blk_line)
                depth += blk_line.count("{") - blk_line.count("}")
                if "{" in blk_line:
                    started = True
                if started and depth == 0:
                    return "\n".join(block)
            return "\n".join(block)  # unterminated — return what we have
    return None


def _proxy_target(text: str, block: str) -> str | None:
    """Return the block's proxy_pass `host:port`, resolving a `set $var value;` host."""
    m = re.search(r"proxy_pass\s+https?://([^/;\s]+)", block)
    if m is None:
        return None
    target = m.group(1)
    if target.startswith("$"):
        var, _, port = target.partition(":")
        s = re.search(rf"set\s+\{var}\s+([^;\s]+);", text)
        if s is None:
            return None
        target = f"{s.group(1)}:{port}"
    return target


def test_mcp_location_block_exists():
    """A `location /mcp` block exists in nginx.conf."""
    text = _read_nginx_conf()
    assert _location_block(text, "/mcp") is not None, (
        "frontend/nginx.conf has no `location /mcp` block — the MCP gateway is "
        "unreachable in prod (POST /mcp would fall through to the SPA)."
    )


def test_mcp_location_block_proxies_to_backend():
    """The /mcp block proxies to the backend upstream (not the SPA fallback)."""
    text = _read_nginx_conf()
    block = _location_block(text, "/mcp")
    assert block is not None, "no /mcp block (see test_mcp_location_block_exists)"
    assert _proxy_target(text, block) == "backend:8001", (
        "the /mcp block must proxy_pass to the backend upstream (backend:8001); "
        f"got block:\n{block}"
    )
    # It must NOT be the SPA fallback.
    assert "try_files" not in block, (
        "the /mcp block must not use try_files (SPA fallback); that returns HTML."
    )
    assert "index.html" not in block, (
        "the /mcp block must not reference index.html (SPA fallback)."
    )


def test_mcp_block_disables_buffering():
    """The streamable-HTTP MCP transport must not be buffered by the proxy."""
    text = _read_nginx_conf()
    block = _location_block(text, "/mcp")
    assert block is not None
    assert "proxy_buffering off" in block or "proxy_buffering off;" in block, (
        "the /mcp block must disable proxy buffering for the streamable-HTTP transport."
    )
