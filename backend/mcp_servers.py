"""# SYSTEM: mcp-servers — external MCP server definitions each user keys themselves.

An admin defines server rows for everyone or for admin groups, a moderator
for their own virtual group; each row picks who holds the key (`auth_mode`):
each user personally (a user_secrets row per (user, server)), one shared key
an admin or moderator sets (stored as Fernet ciphertext on the row), or no
key at all. The turn payload carries the RENDERED Authorization header to
the driver, which mounts the server into the session agent's dsh scope
(harness-driver/plugin/src/mcp-servers.ts — the mount, the read-only ask
gate and the result scrub live THERE, on the driver side of the seam).

Visibility reuses the model-access subjects ('public', 'mod:<uid>',
'group:<gid>' — backend/model_access.py user_subjects), and a personal key
reuses the vault (SYSTEM: user-secrets): one row per (user, server) with
`hosts` bound to the server's CURRENT host at save time.

# ARCH: the transport is Authorization-only — the key travels as
# `Authorization: <scheme> <key>` and no other header can carry it, because
# the MCP transport sends headers as a plain requestInit with the SDK's
# fetch default redirects: a cross-origin redirect DROPS Authorization
# (fetch spec) but KEEPS custom headers such as X-Api-Key — a key in a
# custom header would reach the redirect target the operator never
# approved. Bearer and Basic servers work; X-Api-Key servers wait for a
# transport that can pin redirects.

# INVARIANT(security): a decrypted MCP key — personal or shared — exists only
# inside this module's render/check paths and the payload's mcp_servers entry.
# Why: /followup is driver-secret-gated like ai_api_key, but the log,
# frames and REST responses are not; the cabinet and admin views answer
# is_set/needs_reenter (the shared key only as shared_key_set), the shared
# key is stored as ciphertext alone, and the driver's scrub replaces each
# secret_values entry with [secret] in every mcp__ tool result, so an
# unscrubbed echo is the one leak path.
"""
from __future__ import annotations

import base64
import logging
import re
import uuid
from urllib.parse import quote, urlsplit

import egress_guard
import http_clients
import httpx
import user_secrets
from mcp.client.session import ClientSession
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)
from mcp.shared.exceptions import MCPError
from model_access import moderator_subject, user_subjects
from surrealdb import AsyncSurreal

from access import is_instance_admin
from db import extract_id

logger = logging.getLogger(__name__)

# dsh's own serverName contract (SERVER_NAME_PATTERN in the mcp-client) — also
# the mcp__<server>__<tool> prefix. One home, mirrored from the driver's
# mcp-servers.ts SERVER_NAME_RE.
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: Who holds the key: each user personally, one shared key, or nobody.
AUTH_MODES = ("personal", "shared", "none")

#: The scheme prefixed to the key in Authorization. A token scheme per RFC
#: grammar characters; '' = the key is sent verbatim.
AUTH_SCHEME_RE = re.compile(r"^[A-Za-z0-9._~+/-]{0,32}$")

_MAX_TITLE = 200

#: The connection check's per-exchange bound (the plain POST and the SDK
#: handshake each get one).
_CHECK_TIMEOUT_S = 8.0

#: The longest server error message a check verdict relays.
_MAX_DETAIL = 200


class McpServerError(ValueError):
    """A definition or key refused at a gate (maps to HTTP 422)."""


def parse_server_url(url: str) -> tuple[str, int | None]:
    """(host, port) of a server URL after the shape gates: https only, a real
    host, no userinfo — the same three the http_request tool enforces, because
    the rendered credential rides this URL's TLS."""
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise McpServerError(f"invalid URL: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise McpServerError("only https:// URLs are supported")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise McpServerError("the URL has no host")
    if parts.username or parts.password:
        raise McpServerError(
            "userinfo in the URL is not supported (the credential rides the "
            "Authorization header)",
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise McpServerError(f"invalid URL port: {exc}") from exc
    return host, port


def validate_definition(
    *, name: str, title: str, url: str, auth_mode: str,
    auth_scheme: str, help_url: str | None,
) -> dict:
    """One server definition → its normalized row fields, or McpServerError.

    Pure (no db): subjects and the egress check are the route's job.
    """
    if not SERVER_NAME_RE.fullmatch(name):
        raise McpServerError(
            "name must match [A-Za-z0-9_-]{1,32} (it prefixes every tool the server ships)",
        )
    title = (title or "").strip()
    if len(title) > _MAX_TITLE:
        raise McpServerError(f"title must be at most {_MAX_TITLE} characters")
    host, port = parse_server_url(url)
    if auth_mode not in AUTH_MODES:
        raise McpServerError(f"auth_mode must be one of {list(AUTH_MODES)}")
    auth_scheme = (auth_scheme or "").strip()
    if not AUTH_SCHEME_RE.fullmatch(auth_scheme):
        raise McpServerError(
            "auth_scheme must match ^[A-Za-z0-9._~+/-]{0,32}$ (empty = the key is sent verbatim)",
        )
    help_url = (help_url or "").strip() or None
    if help_url is not None and not help_url.startswith(("http://", "https://")):
        raise McpServerError("help_url must be an http(s) URL")
    return {
        "name": name,
        "title": title or name,
        "url": url.strip(),
        "auth_mode": auth_mode,
        "auth_scheme": auth_scheme,
        "help_url": help_url,
        "host": host,
        "port": port,
    }


def normalize_key(value: str, scheme: str) -> str:
    """The one normalizer for BOTH key paths (a personal PUT and the shared
    save): trim + printable-ASCII via user_secrets.normalize_value, strip a
    leading `<scheme> ` case-insensitively when the scheme is non-empty (a
    key pasted with its Bearer prefix must not end up `Bearer Bearer`), and
    refuse under 8 chars.

    # INVARIANT(security): the refusal never quotes the value.
    # Why: the driver's scrub rewrites every occurrence of a key in tool
    # results, so a 2-char value would mangle ordinary results — and a short
    # value echoed back in an error would sail past the same scrub.
    """
    try:
        value = user_secrets.normalize_value(value)
    except user_secrets.SecretValidationError as exc:
        raise McpServerError(str(exc)) from exc
    if scheme:
        prefix = f"{scheme} "
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix):].strip()
    if len(value) < 8:
        raise McpServerError("the key must be at least 8 characters")
    return value


async def check_egress(host: str) -> None:
    """The save/build-time guard dial: the server's host must resolve public
    (or inside OUTBOUND_ALLOW_CIDRS) — McpServerError on refusal."""
    try:
        await egress_guard.resolve_outbound_host(host)
    except egress_guard.EgressRefused as exc:
        raise McpServerError(str(exc)) from exc


# ─── Row CRUD ─────────────────────────────────────────────────────────────────


async def list_all_rows(db: AsyncSurreal) -> list[dict]:
    """Every server row, name-sorted (the admin list)."""
    rows = await db.query("SELECT * FROM mcp_servers ORDER BY name")
    return list(rows or [])


async def get_row(db: AsyncSurreal, server_id: str) -> dict | None:
    rows = await db.query(
        "SELECT * FROM type::record('mcp_servers', $id)", {"id": server_id},
    )
    return rows[0] if rows else None


async def create_row(db: AsyncSurreal, data: dict) -> dict | None:
    """Insert one row; None when the name UNIQUE index rejects it."""
    try:
        rows = await db.query(
            "CREATE type::record('mcp_servers', $id) CONTENT $data RETURN AFTER",
            {"id": uuid.uuid4().hex, "data": data},
        )
    except Exception as exc:  # noqa: BLE001 — same classifier as user_secrets.create_row
        msg = str(exc).lower()
        if "index" in msg and "already contains" in msg:
            return None
        raise
    return rows[0] if rows else None


async def update_row(db: AsyncSurreal, server_id: str, sets: dict) -> None:
    if not sets:
        return
    pairs = ", ".join(f"{k} = ${k}" for k in sets)
    await db.query(
        f"UPDATE type::record('mcp_servers', $id) SET {pairs}, "
        "updated_at = time::now()",
        {"id": server_id, **sets},
    )


async def delete_row(db: AsyncSurreal, server_id: str) -> None:
    """Delete the server AND every user's key rows for it — an orphaned
    ciphertext row is a credential left in the database (same why as
    user_secrets.delete_user_rows)."""
    await user_secrets.delete_all_server_rows(db, server_id)
    await db.query("DELETE type::record('mcp_servers', $id)", {"id": server_id})


async def drop_moderator_servers(db: AsyncSurreal, moderator_uid: str) -> None:
    """Delete the moderator's own single-subject servers and strip `mod:<uid>`
    from multi-subject rows, once they are demoted or deleted — the
    mcp_servers twin of model_access.drop_moderator_grants (a re-promoted
    moderator starts with nothing, and their users' keys go with the rows:
    nothing can spend them anymore).

    `subjects[WHERE $this != $s]` walks an array of STRINGS: filtering on a
    field name (`subject`) would read NONE for every element and keep them
    all — the same silent no-op the group strip carried.
    """
    subject = moderator_subject(moderator_uid)
    ids = await db.query(
        "SELECT VALUE meta::id(id) FROM mcp_servers WHERE subjects = [$s]",
        {"s": subject},
    )
    if ids:
        await db.query(
            "DELETE user_secrets WHERE server_id IN $ids", {"ids": list(ids)},
        )
        await db.query(
            "DELETE mcp_servers WHERE subjects = [$s]", {"s": subject},
        )
    await db.query(
        "UPDATE mcp_servers SET subjects = subjects[WHERE $this != $s] "
        "WHERE subjects CONTAINS $s",
        {"s": subject},
    )


async def set_user_disabled(
    db: AsyncSurreal, server_id: str, user_id: str, disabled: bool,
) -> None:
    """The per-user toggle, every auth mode: add/remove the user in the row's
    `disabled_by` (a server with no key can be declined too — its tool
    schemas ride every request otherwise)."""
    if disabled:
        await db.query(
            "UPDATE type::record('mcp_servers', $id) SET "
            "disabled_by = array::union(disabled_by, [$uid]), "
            "updated_at = time::now()",
            {"id": server_id, "uid": user_id},
        )
    else:
        await db.query(
            "UPDATE type::record('mcp_servers', $id) SET "
            "disabled_by = disabled_by[WHERE $this != $uid], "
            "updated_at = time::now()",
            {"id": server_id, "uid": user_id},
        )


async def remove_disabled_user(db: AsyncSurreal, user_id: str) -> None:
    """Remove the user from every server's disabled_by (the user-delete
    cascade — a dead account's uid must not linger in toggle state)."""
    await db.query(
        "UPDATE mcp_servers SET disabled_by = disabled_by[WHERE $this != $uid] "
        "WHERE disabled_by CONTAINS $uid",
        {"uid": user_id},
    )


# ─── Visibility (the model-access subject ladder) ────────────────────────────


async def visible_rows(db: AsyncSurreal, user: dict) -> list[dict]:
    """Enabled servers visible to `user`; an admin sees every enabled row
    (same ladder as model_access.available_models)."""
    rows = await db.query(
        "SELECT * FROM mcp_servers WHERE enabled = true ORDER BY name",
    )
    rows = list(rows or [])
    if is_instance_admin(user):
        return rows
    subjects = set(await user_subjects(db, user["user_id"]))
    return [r for r in rows if subjects & set(r.get("subjects") or [])]


async def visible_row(db: AsyncSurreal, user: dict, server_id: str) -> dict | None:
    """One visible enabled server, or None — the cabinet routes' 404 lookup."""
    row = await get_row(db, server_id)
    if not row or not row.get("enabled"):
        return None
    if is_instance_admin(user):
        return row
    subjects = set(await user_subjects(db, user["user_id"]))
    return row if subjects & set(row.get("subjects") or []) else None


def _row_host(row: dict) -> tuple[str, int | None]:
    """(host, port) of a STORED row — the save gate proved the shape; a row
    that predates a schema change still answers None (skipped, never 500)."""
    try:
        return parse_server_url(row.get("url") or "")
    except McpServerError:
        logger.warning(
            "mcp server %s skipped: its stored url no longer parses",
            row.get("name"),
        )
        return "", None


# ─── Key resolution (the one path for render and check) ─────────────────────


async def _resolve_key(
    db: AsyncSurreal, user_id: str, row: dict, host: str, port: int | None,
) -> tuple[str, str]:
    """(key, failure) for `user_id` riding `row`: the key, or ("", why) with
    failure ∈ {"disabled", "no_key", "needs_reenter", ""}. No network — the
    render and the connection check both resolve before dialing."""
    if user_id and user_id in set(row.get("disabled_by") or []):
        return "", "disabled"
    mode = row.get("auth_mode") or "personal"
    if mode == "none":
        return "", ""
    if mode == "shared":
        value = user_secrets.decrypt_value(row.get("shared_ciphertext") or "")
        if value is None:
            return "", "no_key"
        if row.get("shared_host") != user_secrets.host_key(host, port):
            return "", "needs_reenter"
        return value, ""
    vr = await user_secrets.server_key_row(db, user_id, extract_id(row["id"]))
    if vr is not None:
        value = user_secrets.decrypt_value(vr.get("ciphertext") or "")
        if value is not None:
            if not user_secrets.host_allowed(host, port, list(vr.get("hosts") or [])):
                return "", "needs_reenter"
            return value, ""
    return "", "no_key"


def _auth_headers(row: dict, key: str) -> dict[str, str]:
    """The rendered header set — Authorization only (the module ARCH)."""
    if (row.get("auth_mode") or "personal") == "none":
        return {}
    scheme = row.get("auth_scheme") or ""
    return {"Authorization": f"{scheme} {key}".strip()}


async def checkable_headers(
    db: AsyncSurreal, row: dict, *, user: dict | None,
) -> dict[str, str]:
    """The headers the connection check dials with: the caller's own render
    (`user` set, any mode) or the shared/none render (`user` None — a
    personal server is each user's to check, from the cabinet).

    Raises McpServerError when the check cannot run (no key, stranded key,
    declined server) — the routes answer 422 with exactly that text.
    """
    if (row.get("auth_mode") or "personal") == "personal" and user is None:
        raise McpServerError(
            "a personal-key server is checked from the cabinet, with each user's own key",
        )
    host, port = _row_host(row)
    if not host:
        raise McpServerError("the stored url no longer parses")
    key, failure = await _resolve_key(
        db, user["user_id"] if user else "", row, host, port,
    )
    if failure == "no_key":
        raise McpServerError("no key saved for this server")
    if failure == "needs_reenter":
        raise McpServerError("the url moved to another host — re-enter the key")
    if failure == "disabled":
        raise McpServerError("you declined this server — enable it first")
    if failure:
        raise McpServerError("the server cannot ride this turn")
    return _auth_headers(row, key)


# ─── The turn payload render ─────────────────────────────────────────────────


async def _render_one(db: AsyncSurreal, user_id: str, row: dict) -> dict | None:
    """One server → its payload entry, or None when it cannot ride the turn:
    the user declined it, the key is unset/undecryptable/stranded on another
    host, or the egress guard refuses the host. The key resolves FIRST, with
    no network — a keyless user pays no DNS for a server that cannot ride
    (the egress lookup is per server, sequential, on the turn's path). A
    skipped server costs its tools, nothing else — the turn runs."""
    host, port = _row_host(row)
    if not host:
        return None
    key, failure = await _resolve_key(db, user_id, row, host, port)
    if failure:
        return None
    try:
        await egress_guard.resolve_outbound_host(host)
    except egress_guard.EgressRefused:
        logger.warning(
            "mcp server %s skipped at payload build: host refused by the egress guard",
            row.get("name"),
        )
        return None
    # INVARIANT(security): what leaves here is the user's key in the clear —
    # the payload's mcp_servers entry only, never a log line or a frame.
    # Why: /followup is gated by the driver secret, the backend log is not —
    # the same exposure split the ai_api_key INVARIANT in driver/client.py
    # states for the operator's credentials.
    return {
        "name": row.get("name"),
        "title": row.get("title"),
        "url": row.get("url"),
        "headers": _auth_headers(row, key),
        "secret_values": [key] if key else [],
    }


async def render_for_user(db: AsyncSurreal, user: dict) -> list[dict]:
    """The turn payload's mcp_servers array (backend/routes/chat/
    completions_harness.py): every enabled server visible to the user that
    the user has not declined and whose key resolves for the CURRENT host."""
    out = []
    for row in await visible_rows(db, user):
        entry = await _render_one(db, user["user_id"], row)
        if entry is not None:
            out.append(entry)
    return out


# WHY: positive wording — the model must tell an external twin from this
# project's own tool, and a prohibition alone is ignored; the fork plus the
# default is what moves it.
PROMPT_SECTION_HEADER = (
    "This project's own tools carry no prefix and are the ones to use for any "
    "request about this project — its documents, references, memory and chats. "
    "The MCP servers below are EXTERNAL services the user connected; their "
    "mcp__<server>__ tools act on that service, not on this project. Call one "
    "when the request names that service or needs something only it holds:"
)


def prompt_section(entries: list[dict]) -> str:
    """The model-facing name of each mounted server, appended to the system
    prompt: one line per server — `name — title` plus its mcp__<name>__ tool
    prefix — sorted by name, whitespace-collapsed (a title must not inject a
    prompt line). No entries → "" (the prompt stays byte-identical for a
    user with no servers)."""
    if not entries:
        return ""
    lines = sorted(
        f"{' '.join(str(e.get('name')).split())} — "
        f"{' '.join(str(e.get('title') or e.get('name')).split())} "
        f"(tools: mcp__{e.get('name')}__*)"
        for e in entries
    )
    return (
        "\n\n" + PROMPT_SECTION_HEADER + "\n" + "\n".join(lines)
    )


# ─── The connection check ────────────────────────────────────────────────────


def _walk_exc(exc: BaseException):
    """Every node of an exception tree: the groups' sub-exceptions and the
    causal chain (the mcp client wraps failures in nested ExceptionGroups)."""
    stack, seen = [exc], set()
    while stack:
        node = stack.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        stack.extend(getattr(node, "exceptions", []) or [])
        if node.__cause__ is not None:
            stack.append(node.__cause__)


class _Unauthorized(Exception):
    """Sentinel: the handshake's JSON-RPC error carried a 401/403 status."""


async def _sdk_handshake(url: str, headers: dict[str, str]) -> int:
    """initialize + tools/list through the mcp client; returns the tool
    count. Raises freely — the caller classifies and never re-raises.

    WHY a float timeout: an httpx.Timeout object breaks this mcp build's
    transport arithmetic (TypeError `float + Timeout` under the connect
    task group, measured on gray).
    """
    http_client = create_mcp_http_client(
        headers=headers, timeout=_CHECK_TIMEOUT_S,
    )
    async with streamable_http_client(url, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
    return len(result.tools)


async def check_connection(url: str, headers: dict[str, str]) -> dict:
    """One live `initialize` + `tools/list` against `url` with `headers`:
    `{status: ok|unauthorized|refused|unreachable|protocol, tools: int|None,
    detail: str|None}` — on a non-2xx answer `detail` is the server's own
    JSON-RPC error message scrubbed of the key, else `HTTP <code>` (see
    _server_detail).

    The egress guard runs first; no redirect is followed; the answer NEVER
    carries exception text (an error repr can hold a header value).

    WHY a plain httpx POST classifies first: the mcp==2.2.0 streamable
    client wraps every failure in an ExceptionGroup whose leaf for an
    HTTP-level 401 is a generic MCPError ("Server returned an error
    response") with no status — measured on gray against the Lore gateway —
    so the HTTP status is read from one plain `initialize` POST, and only a
    2xx answer goes on to the SDK client for the real handshake and the
    tool count.
    """
    host, _port = parse_server_url(url)
    try:
        await egress_guard.resolve_outbound_host(host)
    except egress_guard.EgressRefused:
        return {"status": "refused", "tools": None, "detail": None}
    verdict = await _classify_http_post(url, headers)
    if verdict is not None:
        return verdict
    try:
        tools = await _sdk_handshake(url, headers)
    except Exception as exc:  # noqa: BLE001 — classified, never re-raised or quoted
        for node in _walk_exc(exc):
            if isinstance(node, MCPError):
                data = getattr(node.error, "data", None)
                status = data.get("status_code") if isinstance(data, dict) else None
                if str(status) in ("401", "403"):
                    return {"status": "unauthorized", "tools": None, "detail": None}
        logger.warning("mcp connection check failed: %s", type(exc).__name__)
        return {"status": "protocol", "tools": None, "detail": None}
    return {"status": "ok", "tools": tools, "detail": None}


async def _classify_http_post(url: str, headers: dict[str, str]) -> dict | None:
    """One plain `initialize` POST → a status verdict, or None when the
    answer is 2xx and the caller should run the real SDK handshake. 5xx =
    the MCP service is not there; other non-2xx = the endpoint answered but
    is not a usable MCP conversation; transport errors = unreachable."""
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "lore-check", "version": "0"},
        },
    }
    # INVARIANT(security): one exchange, one client.
    # Why: the headers carry the caller's key, and a shared client keeps a
    # cookie jar and a keep-alive pool keyed across exchanges — the exact
    # contract of http_clients.isolated_client.
    client = http_clients.isolated_client(timeout=_CHECK_TIMEOUT_S)
    try:
        resp = await client.post(
            url, json=init,
            headers={"Accept": "application/json, text/event-stream", **headers},
        )
    except httpx.HTTPError:
        return {"status": "unreachable", "tools": None, "detail": None}
    finally:
        await client.aclose()
    if 200 <= resp.status_code < 300:
        return None
    if resp.status_code in (401, 403):
        status = "unauthorized"
    elif resp.status_code >= 500:
        status = "unreachable"
    else:
        status = "protocol"
    return {"status": status, "tools": None, "detail": _server_detail(resp, headers)}


def _secret_forms(headers: dict[str, str]) -> list[str]:
    """The raw, base64 and url-encoded forms of every rendered credential —
    each header value and its bare key (the value minus its scheme) — longest
    first, the same forms the driver scrubs from tool results
    (harness-driver/plugin/src/mcp-servers.ts)."""
    secrets: set[str] = set()
    for value in headers.values():
        secrets.add(value)
        secrets.add(value.split(" ", 1)[-1])
    forms = set()
    for secret in filter(None, secrets):
        forms.update((
            secret,
            base64.b64encode(secret.encode()).decode(),
            quote(secret, safe="-_.!~*'()"),
        ))
    return sorted(forms, key=len, reverse=True)


def _server_detail(resp: httpx.Response, headers: dict[str, str]) -> str:
    """The server's JSON-RPC `error.message` from a refused check, bounded
    and cleaned; any other body (a proxy's HTML error page) is not relayed and
    answers just `HTTP <code>` — a 403 page from a fronting proxy is an
    address filter, not the MCP server judging the key.

    # INVARIANT(security): every rendered credential form is replaced with
    # [secret] before the message leaves this function.
    # Why: the verdict is a REST response, and a shared key is checked from the
    # cabinet by users who must never see it — a server echoing Authorization
    # back in its error text must not hand them the key.
    """
    fallback = f"HTTP {resp.status_code}"
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return fallback
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return fallback
    for form in _secret_forms(headers):
        message = message.replace(form, "[secret]")
    message = " ".join("".join(c if c.isprintable() else " " for c in message).split())
    return message[:_MAX_DETAIL] or fallback


# ─── The cabinet view ─────────────────────────────────────────────────────────


async def _visible_entry(
    db: AsyncSurreal, user_id: str, row: dict, names: dict,
) -> dict:
    """One VISIBLE server for the cabinet: the write-only key state by mode,
    the per-user toggle — never a value."""
    server_id = extract_id(row["id"])
    host, port = _row_host(row)
    mode = row.get("auth_mode") or "personal"
    is_set: bool | None = None
    needs_reenter = False
    if mode == "personal":
        vr = await user_secrets.server_key_row(db, user_id, server_id)
        is_set = bool(
            vr and host and user_secrets.is_set(vr.get("ciphertext"))
            and user_secrets.host_allowed(host, port, list(vr.get("hosts") or []))
        )
        needs_reenter = vr is not None and not is_set
    elif mode == "shared":
        is_set = bool(
            host
            and user_secrets.is_set(row.get("shared_ciphertext") or "")
            and row.get("shared_host") == user_secrets.host_key(host, port)
        )
        needs_reenter = not is_set and bool(row.get("shared_ciphertext"))
    return {
        "id": server_id,
        "name": row.get("name"),
        "title": row.get("title"),
        "host": (f"{host}:{port}" if port else host) if host else "",
        "auth_mode": mode,
        "help_url": row.get("help_url"),
        "created_by": row.get("created_by"),
        "created_by_name": names.get(row.get("created_by"), ""),
        "unavailable": False,
        # The per-user toggle state (every mode); a re-entered key clears it
        # at the PUT.
        "enabled": user_id not in set(row.get("disabled_by") or []),
        "is_set": is_set,
        "needs_reenter": needs_reenter,
    }


def _hidden_entry(row: dict, names: dict) -> dict:
    """A server the caller can no longer see but still holds a key for:
    name, host, delete-only. Ruling: this reveals a revoked server only to
    someone who already holds a key for it."""
    host, port = _row_host(row)
    return {
        "id": extract_id(row["id"]),
        "name": row.get("name"),
        "title": row.get("title"),
        "host": (f"{host}:{port}" if port else host) if host else "",
        "auth_mode": row.get("auth_mode") or "personal",
        "help_url": None,
        "created_by": row.get("created_by"),
        "created_by_name": names.get(row.get("created_by"), ""),
        "unavailable": True,
        "enabled": False,
        "is_set": False,
        "needs_reenter": True,
    }


async def cabinet_view(db: AsyncSurreal, user: dict) -> list[dict]:
    """The caller's servers for the cabinet section: host line, creator, the
    write-only key state, the per-user toggle — never a value. Servers the
    caller can no longer see but still holds a key row for ride as
    `unavailable: true` delete-only cards."""
    user_id = user["user_id"]
    rows = await visible_rows(db, user)
    by_id = {extract_id(r["id"]): r for r in rows}
    hidden_ids = [
        sid for sid in (await db.query(
            "SELECT VALUE server_id FROM user_secrets "
            "WHERE user_id = $uid AND server_id != NONE",
            {"uid": user_id},
        ) or [])
        if sid not in by_id
    ]
    hidden_rows = await db.query(
        "SELECT * FROM mcp_servers WHERE meta::id(id) IN $ids ORDER BY name",
        {"ids": hidden_ids},
    ) if hidden_ids else []
    creator_ids = sorted({
        r.get("created_by") for r in [*rows, *hidden_rows] if r.get("created_by")
    })
    names = {
        r["uid"]: r["name"]
        for r in await db.query(
            "SELECT meta::id(id) AS uid, name FROM users WHERE meta::id(id) IN $ids",
            {"ids": creator_ids},
        ) or []
    }
    visible = [await _visible_entry(db, user_id, r, names) for r in rows]
    hidden = [_hidden_entry(r, names) for r in hidden_rows or []]
    return visible + hidden
