"""Tool-API — personal secrets: list_secrets + http_request (SYSTEM: user-secrets).

The agent-facing half of the vault (backend/user_secrets.py holds the rows):
`list_secrets` shows the caller's enabled rows (name/description/hosts — never
a value); `http_request` spends one row against a host it is BOUND to. The
plaintext exists only between `decrypt_value` and the outbound header here,
and everything the call returns — status text, headers, body, error text — is
scrubbed of the value and its common encodings before anything else sees it.

# ARCH (dsh comparison, recorded per the rented-brain rule): dsh ships no
# per-user credential vault and no egress-guarded fetch — `vendor/dsh/`
# web_search/fetch machinery is provider-side, so a Lore layer is justified by
# naming what dsh cannot state: host-bound personal keys owned by a Lore user,
# spent under Lore's key policy with the value never entering the harness
# stream.

# ARCH: this domain is console-key-gated (`_require_console_key`, the sandbox
# predicate) — internal whole-project keys only, so external MCP clients and
# subtree-scoped keys never reach the user's personal keys. No project-access
# check rides on top: the call spends the CALLER's own stored key, not a
# project resource, and the console key already names the in-app agent.
# sandbox.{files,transport} are imported at MODULE level here (not lazily like
# skills/imports do): they are leaves of this package's graph (access +
# settings + fastapi), the lazy form was only the cross-domain courtesy, and
# the import-shape ratchet prices every in-function import.
"""

import asyncio
import base64
import uuid
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit

import egress_guard
import http_clients
import httpx
import user_secrets
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from db import get_db
from models import ToolHttpRequest, ToolListSecrets
from routes.tool_api._common import _refuse_unconfirmable
from routes.tool_api.sandbox.files import (
    _require_console_key as _gate_console_key,
)
from routes.tool_api.sandbox.files import write_workspace_file
from routes.tool_api.sandbox.transport import (
    MAX_OUTPUT_CHARS,
    _require_sandbox_configured,
)
from routes.tool_api_telemetry import track_agent_tool

#: One exchange's whole-life budget: resolve, connect, send, read to the end.
REQUEST_TIMEOUT_S = 30.0
#: Agent-supplied request body ceiling (utf-8 bytes).
MAX_REQUEST_BODY_BYTES = 1024 * 1024
#: Response download ceiling. Over it is a 413, not a truncated read: a
#: half-open 20 MB stream is not a result an agent can use.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
#: Methods that only READ the bound host — they run without an ask.
_SILENT_METHODS = frozenset({"GET", "HEAD"})
#: Header names the agent can never set: the dial's Host and the credential
#: header are server-owned; length and framing are httpx's; accept-encoding
#: because a coding httpx cannot decode would carry a reflected value past the
#: scrub; the method overrides because they turn a silent GET into a write.
_AGENT_FORBIDDEN_HEADERS = frozenset({
    "host", "authorization", "content-length", "transfer-encoding",
    "accept-encoding", "x-http-method-override", "x-http-method",
    "x-method-override",
})


def _require_console_key(ctx: dict) -> None:
    """The pack's ONE principal gate — the sandbox predicate, reached through
    its single home so the two surfaces cannot drift.

    # INVARIANT(security): `http_request`/`list_secrets` execute ONLY for the
    # internal whole-project key (internal=True, scope_root=='').
    # Why: this excludes external mcp-gateway keys (a third-party MCP client
    # must never spend the user's personal keys) and user-minted subtree-scoped
    # keys (a key deliberately narrowed to one tree must not silently widen
    # into the owner's credential vault).
    """
    _gate_console_key(ctx)


# ─── list_secrets ─────────────────────────────────────────────────────────────


@track_agent_tool("list_secrets")
async def tool_list_secrets(
    body: ToolListSecrets = Body(default_factory=ToolListSecrets),
    ctx: dict = Depends(get_agent_context),
):
    """The caller's enabled personal keys as {name, description, hosts} — the
    model-facing catalog `http_request` spends from. The value is structurally
    absent: it never leaves the Fernet ciphertext and this domain's dial."""
    _require_console_key(ctx)
    db = await get_db()
    rows = await user_secrets.list_rows(db, ctx["user_id"])
    return {"secrets": [
        {
            "name": r.get("name"),
            "description": r.get("description") or "",
            "hosts": list(r.get("hosts") or []),
        }
        for r in rows if r.get("enabled")
    ]}


# ─── http_request: the pieces ─────────────────────────────────────────────────


def _scrub_forms(value: str) -> list[str]:
    """The encodings a reflection could carry the value in: raw, both base64
    alphabets, and the url-encoded variants (with and without reserved chars)."""
    if not value:
        return []
    raw_b = value.encode("utf-8")
    forms = {
        value,
        base64.b64encode(raw_b).decode(),
        base64.urlsafe_b64encode(raw_b).decode(),
        quote(value),
        quote(value, safe=""),
        quote_plus(value),
    }
    return [f for f in forms if f]


def _scrub(text: str, forms: list[str]) -> str:
    for f in forms:
        text = text.replace(f, "[secret]")
    return text


def _scrub_bytes(data: bytes, forms: list[str]) -> bytes:
    for f in forms:
        data = data.replace(f.encode("utf-8"), b"[secret]")
    return data


def _parse_target_url(url: str) -> tuple[str, int | None, str]:
    """(host, port, path+query) of the agent's URL, after the shape gates.

    # INVARIANT(security): https only, no userinfo, a real host — the shape
    # checks run BEFORE the host binding.
    # Why: `http://` would send the key in cleartext, and userinfo
    # (`https://k@host/`) is a second credential channel the handler does not
    # own.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid URL: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise HTTPException(
            status_code=422, detail="only https:// URLs are supported",
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=422, detail="the URL has no host")
    if parts.username or parts.password:
        raise HTTPException(
            status_code=422,
            detail="userinfo in the URL is not supported (the credential "
                   "header is injected server-side)",
        )
    try:
        # None when absent; the https default 443 folds in host_key. `.port`
        # RAISES on an out-of-range port — that is URL shape, so it maps to
        # the same 422, never an unhandled 500 on an agent-crafted URL.
        port = parts.port
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid URL port: {exc}",
        ) from exc
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def _outbound_headers(
    agent_headers: dict[str, str], row: dict, value: str,
) -> dict[str, str]:
    """Merge the agent's headers with the injected credential — the server
    owns Host, Authorization and the row's header_name, so the agent's copy of
    any of them is dropped and the injected header always wins."""
    header_name = (row.get("header_name") or "").strip()
    banned = _AGENT_FORBIDDEN_HEADERS | ({header_name.lower()} if header_name else set())
    out = {
        k: v for k, v in agent_headers.items()
        if k.strip().lower() not in banned
    }
    if header_name and not (row.get("query_param") or ""):
        template = row.get("header_template") or user_secrets.DEFAULT_HEADER_TEMPLATE
        out[header_name] = user_secrets.render_header_template(template, value)
    return out


def _inject_query_key(path: str, query_param: str, value: str) -> str:
    """Query mode: drop the agent's own `query_param` (compared DECODED, so
    `?k%65y=` is stripped too) and append the key under that name.

    # INVARIANT(security): the server's parameter is the only one of its name.
    # Why: a duplicate the agent planted could be the one the host reads,
    # turning the bound call into a call under a key of the agent's choosing.
    """
    base, _, query = path.partition("?")
    kept = [
        (k, v) for k, v in parse_qsl(query, keep_blank_values=True)
        if k != query_param
    ]
    kept.append((query_param, value))
    return f"{base}?{urlencode(kept, quote_via=quote, safe='')}"


def _outbound_request(
    body: ToolHttpRequest, row: dict, value: str,
    host: str, port: int | None, path: str,
) -> tuple[dict[str, str], str]:
    """(headers, path) as dialled: the credential in its header, or — query
    mode — appended to the path; Host pinned to the bound name."""
    headers = _outbound_headers(body.headers or {}, row, value)
    query_param = row.get("query_param") or ""
    if query_param:
        path = _inject_query_key(path, query_param, value)
    headers["Host"] = host if port is None or port == 443 else f"{host}:{port}"
    return headers, path


async def _resolve_secret(db, ctx: dict, name: str) -> tuple[dict, str]:
    """(row, plaintext) for the caller's enabled secret `name` — the handler's
    only decrypt site, so the value's lifetime is this one call."""
    row = await user_secrets.get_row(db, ctx["user_id"], name)
    if not row or not row.get("enabled"):
        raise HTTPException(
            status_code=404,
            detail=f"no personal key named {name!r} "
                   "(list_secrets shows the available ones)",
        )
    # An MCP server key row (see SYSTEM: mcp-servers) is spent by the turn
    # payload's renderer against the server's own url, never by an
    # agent-picked dial: letting http_request resolve it would hand the
    # agent the key for any path of that host, which is exactly the
    # exfiltration the hosts binding refuses.
    if row.get("server_id"):
        raise HTTPException(
            status_code=403,
            detail=f"{name!r} is an MCP server key — the chat's MCP servers "
                   "spend it, not http_request",
        )
    value = user_secrets.decrypt_value(row.get("ciphertext") or "")
    if value is None:
        raise HTTPException(
            status_code=409,
            detail="the stored key can no longer be decrypted — ask the user "
                   "to re-enter it in the cabinet (Lore settings)",
        )
    return row, value


async def _exchange(
    *, method: str, host: str, port: int | None, path: str,
    headers: dict[str, str], body_bytes: bytes | None,
) -> tuple[httpx.Response, bytes]:
    """One request + its whole body, at an address the egress guard approved.

    # INVARIANT(security): the connection goes to the RESOLVED ip with
    # `sni_hostname`/Host pinned to the NAME.
    # Why: that closes DNS rebinding — a name that re-resolves to a refused
    # address between the guard's check and the connect changes nothing,
    # because the dial never re-resolves. TLS verification stays ON and checks
    # the name (the cert the ip presents must be valid for the host the user
    # bound the key to).
    """
    ips = await egress_guard.resolve_outbound_host(host)
    ip = ips[0]
    netloc = f"[{ip}]" if ":" in ip else ip
    if port is not None:
        netloc += f":{port}"
    async with http_clients.isolated_client(timeout=REQUEST_TIMEOUT_S) as client:
        request = client.build_request(
            method, f"https://{netloc}{path}", headers=headers,
            content=body_bytes, extensions={"sni_hostname": host},
        )
        # stream=True: the body is read by _read_capped chunk-by-chunk, so the
        # download cap can abort mid-transfer — a non-stream send buffers the
        # whole response inside httpx before this handler ever sees a byte.
        resp = await client.send(request, follow_redirects=False, stream=True)
        return resp, await _read_capped(resp)


async def _read_capped(resp: httpx.Response) -> bytes:
    """Stream the response, refusing the MOMENT the cap is crossed — a cap
    checked after a full read is no cap at all: the bytes are already in
    backend memory, and only the 30 s timeout stands between an agent-pointed
    huge response and the process."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            await resp.aclose()
            raise HTTPException(
                status_code=413,
                detail=f"response over the {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB "
                       "download cap",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _dial(
    body: ToolHttpRequest, host: str, port: int | None, path: str,
    headers: dict[str, str], forms: list[str],
) -> tuple[httpx.Response, bytes]:
    """The guarded exchange + error mapping: every error text is scrubbed before
    it becomes an HTTPException detail (the driver proxies detail strings into
    the model's context).

    # INVARIANT(security): an encoding error's text is never served.
    # Why: the HTTP layer quotes the illegal header in it, credential included,
    # and a repr() of the value is not a form the scrub can match.
    """
    body_bytes = body.body.encode("utf-8") if body.body is not None else None
    if body_bytes and len(body_bytes) > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"request body over the {MAX_REQUEST_BODY_BYTES}-byte cap",
        )
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            return await _exchange(
                method=body.method, host=host, port=port, path=path,
                headers=headers, body_bytes=body_bytes,
            )
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise HTTPException(
            status_code=504,
            detail=f"the host did not finish answering within {REQUEST_TIMEOUT_S}s",
        ) from exc
    except (httpx.LocalProtocolError, httpx.InvalidURL, UnicodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="the URL or a header cannot be sent: use printable ASCII",
        ) from exc
    except (httpx.HTTPError, egress_guard.EgressRefused, OSError) as exc:
        detail = _scrub(str(exc) or exc.__class__.__name__, forms)
        code = 403 if isinstance(exc, egress_guard.EgressRefused) else 502
        raise HTTPException(status_code=code, detail=detail) from exc


# ─── http_request ─────────────────────────────────────────────────────────────


def _require_bound_host(row: dict, name: str, host: str, port: int | None) -> None:
    """The exfiltration guard: this call's (host, port) must be one of the
    row's exact save-time bindings — the agent can read a bound host's data
    with the key but never pick a destination of its own."""
    if not user_secrets.host_allowed(host, port, list(row.get("hosts") or [])):
        raise HTTPException(
            status_code=403,
            detail=f"the key {name!r} is not bound to {host}"
                   + (f":{port}" if port else "")
                   + "; it can only be used with its saved hosts",
        )


async def _response_result(
    resp: httpx.Response, data: bytes, body: ToolHttpRequest,
    forms: list[str], ctx: dict,
) -> dict:
    """The scrubbed, capped result: inline text up to MAX_OUTPUT_CHARS, or —
    with `save` — the backend-derived workspace path (`inbox/http/{uuid}/
    response.bin`; the agent never names the path, write_workspace_file
    relies on it)."""
    data = _scrub_bytes(data, forms)
    result: dict = {
        "status_code": resp.status_code,
        "reason": _scrub(resp.reason_phrase or "", forms),
        "headers": {
            _scrub(str(k), forms): _scrub(str(v), forms)
            for k, v in resp.headers.items()
        },
        "url": _scrub(body.url, forms),
    }
    if body.save:
        rel = f"inbox/http/{uuid.uuid4().hex}/response.bin"
        result["saved_path"] = await write_workspace_file(ctx, rel, data)
        result["bytes"] = len(data)
        return result
    text = _scrub(data.decode("utf-8", errors="replace"), forms)
    result["body"] = text[:MAX_OUTPUT_CHARS]
    result["truncated"] = len(text) > MAX_OUTPUT_CHARS
    return result


@track_agent_tool("http_request")
async def tool_http_request(
    body: ToolHttpRequest = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Call a host one of the user's personal keys is BOUND to, with the key
    injected server-side. GET/HEAD run silently; every other method needs the
    user's approval on THIS call (the driver-attested `allowed-once` verdict) —
    `auto` never substitutes for it, because a foreign write under the user's
    name has no history to roll back.

    # INVARIANT(security): a user secret never enters the sandbox, a log line,
    # a frame, a REST response or the model context.
    # Why: sandbox_bash returns stdout to the model, so any secret in a
    # command's env can be printed; the box is one shared unix account with
    # open internet. On THIS handler that means the value exists only in
    # `value` and the outbound header; `_dial` scrubs every error text and
    # `_response_result` every served byte BEFORE anything else sees them;
    # a redirect is returned, never followed (a 3xx Location cannot carry the
    # key to a second host).
    """
    _require_console_key(ctx)
    db = await get_db()
    row, value = await _resolve_secret(db, ctx, body.secret)

    host, port, path = _parse_target_url(body.url)
    _require_bound_host(row, body.secret, host, port)

    # A silent method carries no body: a read that sends data is a write, and
    # a write gets the ask.
    if body.method in _SILENT_METHODS and body.body is not None:
        raise HTTPException(
            status_code=422, detail=f"{body.method} carries no body; use POST",
        )

    # The per-call verdict gate — deliberately NOT _resolve_apply_or_force:
    # the user's auto preference and the MCP force flag must not widen a
    # foreign write; only the driver-attested approval of THIS call applies.
    if body.method not in _SILENT_METHODS and ctx.get("verdict") != "allowed-once":
        _refuse_unconfirmable("http_request")

    if body.save:
        # Fail fast, before the dial: a save that cannot land anywhere must
        # not spend the key against the host first.
        await _require_sandbox_configured()

    headers, path = _outbound_request(body, row, value, host, port, path)
    forms = _scrub_forms(value)
    resp, data = await _dial(body, host, port, path, headers, forms)
    return await _response_result(resp, data, body, forms, ctx)
