"""# SYSTEM: user-secrets — a user's personal API keys, encrypted at rest.

The cabinet routes write rows (name, description, bound hosts, header shape,
Fernet ciphertext); the http_request tool decrypts a row to call one of its
bound hosts. The plaintext value exists only inside this module's decrypt and
the tool handler — never in a REST response, a frame or the model context.
"""
from __future__ import annotations

import base64
import re
import uuid

import instance_secret
from cryptography.fernet import Fernet, InvalidToken
from surrealdb import AsyncSurreal

import config
from db import extract_id

# The key file is a dedicated one under the storage root, deliberately NOT
# derived from the JWT SECRET_KEY: rotating sessions must never brick stored
# secrets (and a leaked session secret must not decrypt them).
_KEY_FILE = ".lore/user_secrets_key"

DEFAULT_HEADER_NAME = "Authorization"
DEFAULT_HEADER_TEMPLATE = "Bearer {{value}}"
DEFAULT_PORT = 443
MAX_HOSTS = 32
VALUE_PLACEHOLDER = "{{value}}"
VALUE_B64_PLACEHOLDER = "{{value_b64}}"

# Name: `[A-Za-z0-9_-]{1,64}`; `--` refused because it is the SurrealQL
# comment marker — every query is parameterized, but a name that can never
# open a comment keeps that true by construction, not by auditing each site.
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# host[:port], hostname/ip chars only — no scheme, no path. An optional `*.`
# prefix makes a subdomain mask (see normalize_host).
_HOSTPORT_RE = re.compile(r"^(\*\.)?([A-Za-z0-9.-]+?)(?::(\d{1,5}))?$")
MASK_PREFIX = "*."
# A query-mode parameter name: travels unencoded in the URL.
_QUERY_PARAM_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# What a header line can carry: printable ASCII.
_HEADER_TEXT_RE = re.compile(r"[\x20-\x7e]+")
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9-]{1,128}")
# Headers the dial owns: a key put in one of them would never reach the host.
_RESERVED_HEADER_NAMES = frozenset({"host", "content-length", "transfer-encoding"})


class SecretValidationError(ValueError):
    """A name/hosts value refused at the write gate (maps to HTTP 422)."""


def validate_name(name: str) -> str:
    """Accept a user-chosen secret name or raise SecretValidationError."""
    if not SECRET_NAME_RE.fullmatch(name) or "--" in name:
        raise SecretValidationError(
            "name must match [A-Za-z0-9_-]{1,64} and contain no '--'",
        )
    return name


def normalize_value(value: str) -> str:
    """Trim a pasted key; refuse what cannot travel in a header.

    # INVARIANT(security): a stored value is printable ASCII, and this refusal
    # never quotes it.
    # Why: the HTTP layer repr()s an illegal header into its error text, and the
    # repr of a control char no longer matches the scrub's forms — a key pasted
    # with a trailing tab would reach the model and the log.
    """
    stripped = value.strip()
    if not _HEADER_TEXT_RE.fullmatch(stripped):
        raise SecretValidationError(
            "the key must be printable ASCII (it travels in an HTTP header or URL)",
        )
    return stripped


def normalize_header_name(header_name: str) -> str:
    """A header name, "" folding to the default — see normalize_header_template."""
    name = header_name.strip() or DEFAULT_HEADER_NAME
    if not _HEADER_NAME_RE.fullmatch(name) or name.lower() in _RESERVED_HEADER_NAMES:
        raise SecretValidationError(f"invalid header name: {name!r}")
    return name


def normalize_header_template(template: str) -> str:
    """A header template carrying the key.

    Cleared header fields normalize to the server defaults HERE, not at the
    dial: public_view masks an empty stored value as the default (`or DEFAULT`),
    so a stored "" would display "Authorization" while the http_request handler
    injects nothing — display and dial must not diverge. The same goes for a
    template without the placeholder: the dial would send no key at all.
    """
    text = template.strip() or DEFAULT_HEADER_TEMPLATE
    has_placeholder = VALUE_PLACEHOLDER in text or VALUE_B64_PLACEHOLDER in text
    if not has_placeholder or not _HEADER_TEXT_RE.fullmatch(text):
        raise SecretValidationError(
            f"the header template must contain {VALUE_PLACEHOLDER} or "
            f"{VALUE_B64_PLACEHOLDER} and be printable ASCII",
        )
    return text


def render_header_template(template: str, value: str) -> str:
    """Substitute the value (raw and/or standard base64 of its utf-8 bytes)."""
    b64 = base64.b64encode(value.encode("utf-8")).decode()
    return template.replace(VALUE_B64_PLACEHOLDER, b64).replace(VALUE_PLACEHOLDER, value)


def normalize_query_param(query_param: str) -> str:
    """'' (header mode) or a query parameter name the dial appends the key as."""
    name = query_param.strip()
    if name and not _QUERY_PARAM_RE.fullmatch(name):
        raise SecretValidationError(
            "query parameter name must match [A-Za-z0-9_.-]{1,64}",
        )
    return name


def host_key(host: str, port: int | None = None) -> str:
    """(host, port) → canonical binding key: lowercase host[:port], :443 stripped."""
    h = host.lower().rstrip(".")
    if not port or port == DEFAULT_PORT:
        return h
    return f"{h}:{port}"


def normalize_host(host: str) -> str:
    """One `host[:port]` or `*.domain[:port]` entry → canonical key form, or
    SecretValidationError.

    A mask `*.example.com` matches any subdomain at any depth, never the apex.
    # INVARIANT(security): a mask names at least two labels after `*.` and `*`
    # appears only as the whole leftmost label.
    # Why: `*` or `*.com` would bind the key to the whole internet (or a whole
    # TLD) — the binding stops being an exfiltration guard.
    """
    m = _HOSTPORT_RE.match(host.strip())
    if not m:
        raise SecretValidationError(f"invalid host (expected host[:port]): {host!r}")
    mask, h = m.group(1), m.group(2)
    port = int(m.group(3)) if m.group(3) else None
    if port is not None and not 1 <= port <= 65535:
        raise SecretValidationError(f"invalid port in host: {host!r}")
    labels = h.lower().rstrip(".").split(".")
    if mask and (len(labels) < 2 or not all(labels)):
        raise SecretValidationError(
            f"a host mask needs a domain of at least two labels: {host!r}",
        )
    return (MASK_PREFIX if mask else "") + host_key(h, port)


def validate_hosts(hosts: list[str]) -> list[str]:
    """The save-time binding gate: exact-match entries, deduped, order-stable.

    # INVARIANT(security): a secret is bound to its hosts when it is saved,
    # and the agent can never change them.
    # Why: an agent that picks the destination can send the key to its own
    # server without ever reading it — the binding is the exfiltration guard.
    # Only the cabinet routes (a cookie session) call this; no tool writes
    # `hosts`.
    """
    if not hosts:
        raise SecretValidationError("at least one host is required")
    if len(hosts) > MAX_HOSTS:
        raise SecretValidationError(f"at most {MAX_HOSTS} hosts per secret")
    return list(dict.fromkeys(normalize_host(h) for h in hosts))


def hosts_changed(row: dict, hosts: list[str]) -> bool:
    """Does this canonical list bind the key anywhere other than the row does?"""
    return set(hosts) != {normalize_host(h) for h in row.get("hosts") or []}


def host_allowed(host: str, port: int | None, bound_hosts: list[str]) -> bool:
    """Is (host, port) one of the row's bindings — an exact entry, or a
    `*.domain` mask it is a subdomain of with the same port? (Per request.)"""
    key = host_key(host, port)
    bound = {normalize_host(h) for h in bound_hosts}
    if key in bound:
        return True
    return any(
        key.endswith("." + b[len(MASK_PREFIX):])
        for b in bound if b.startswith(MASK_PREFIX)
    )


def _fernet() -> Fernet:
    """Fernet keyed by the persisted per-instance key file (created on first use)."""
    # ensure_key persists token_hex(32); Fernet wants the 32 bytes in urlsafe
    # base64 — same entropy, Fernet's expected encoding.
    key_hex = instance_secret.ensure_key(config.STORAGE_PATH / _KEY_FILE)
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(key_hex)))


def encrypt_value(plain: str) -> str:
    """The only writer of `ciphertext`."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str | None:
    """The only plaintext reader outside the tool handler.

    # INVARIANT(security): a user secret never enters the sandbox, a log line,
    # a frame, a REST response or the model context.
    # Why: sandbox_bash returns stdout to the model, so any secret in a
    # command's env can be printed; the box is one shared unix account with
    # open internet. The ONLY plaintext reader is the http_request tool
    # handler, which injects the header directly into the outbound request and
    # scrubs the value from everything it returns.

    Returns None when the row can no longer be decrypted (key file lost in a
    restore) — that state is served as "not set / re-enter key", never as an
    error and never as an empty secret.
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, TypeError):
        return None


def is_set(ciphertext: str | None) -> bool:
    """A row counts as set only when its ciphertext decrypts."""
    return bool(ciphertext) and decrypt_value(ciphertext) is not None


# ─── Row CRUD (the cabinet routes are the only writers) ──────────────────────


async def list_rows(db: AsyncSurreal, user_id: str) -> list[dict]:
    """The caller's PLAIN rows (server_id IS NONE), name-sorted.

    MCP variable rows (SYSTEM: mcp-servers) are deliberately absent: the
    cabinet list and the list_secrets tool must not show them, and every other
    consumer of this function is about user-chosen names.
    """
    rows = await db.query(
        "SELECT * FROM user_secrets WHERE user_id = $uid "
        "AND server_id IS NONE ORDER BY name",
        {"uid": user_id},
    )
    return list(rows or [])


def public_view(row: dict) -> dict:
    """The REST projection — every field EXCEPT the value/ciphertext."""
    return {
        "name": row.get("name"),
        "description": row.get("description") or "",
        "hosts": list(row.get("hosts") or []),
        "header_name": row.get("header_name") or DEFAULT_HEADER_NAME,
        "header_template": row.get("header_template") or DEFAULT_HEADER_TEMPLATE,
        "query_param": row.get("query_param") or "",
        "enabled": bool(row.get("enabled")),
        "is_set": is_set(row.get("ciphertext")),
        "updated_at": row.get("updated_at"),
    }


async def create_row(
    db: AsyncSurreal,
    user_id: str,
    name: str,
    value: str,
    description: str,
    hosts: list[str],
    header_name: str = DEFAULT_HEADER_NAME,
    header_template: str = DEFAULT_HEADER_TEMPLATE,
    query_param: str = "",
) -> dict | None:
    """Insert one row; None when the (user_id, name) UNIQUE index rejects it."""
    data = {
        "user_id": user_id,
        "name": name,
        "description": description,
        "ciphertext": encrypt_value(value),
        "hosts": hosts,
        "header_name": header_name,
        "header_template": header_template,
        "query_param": query_param,
        "enabled": True,
    }
    try:
        rows = await db.query(
            "CREATE type::record('user_secrets', $id) CONTENT $data RETURN AFTER",
            {"id": uuid.uuid4().hex, "data": data},
        )
    except Exception as exc:  # noqa: BLE001 — the UNIQUE index violation is the expected 409
        # SurrealDB's wording: "Database index `…` already contains […]". Both
        # markers, not bare "index": a foreign error that happens to mention
        # an index (e.g. "index file locked") must propagate, not read as
        # "this name already exists".
        msg = str(exc).lower()
        if "index" in msg and "already contains" in msg:
            return None
        raise
    return rows[0] if rows else None


async def get_row(db: AsyncSurreal, user_id: str, name: str) -> dict | None:
    """One row by (user_id, name) — the PATCH/DELETE lookup and the tool's resolve."""
    rows = await db.query(
        "SELECT * FROM user_secrets WHERE user_id = $uid AND name = $name LIMIT 1",
        {"uid": user_id, "name": name},
    )
    return rows[0] if rows else None


async def update_row(db: AsyncSurreal, row_id: str, sets: dict) -> None:
    """Apply validated field updates. Keys are the route's fixed column names;
    a `ciphertext` key only ever carries a fresh encrypt_value() result."""
    if not sets:
        return
    pairs = ", ".join(f"{k} = ${k}" for k in sets)
    await db.query(
        f"UPDATE type::record('user_secrets', $id) SET {pairs}, "
        "updated_at = time::now()",
        {"id": row_id, **sets},
    )


async def delete_row(db: AsyncSurreal, row_id: str) -> None:
    await db.query("DELETE type::record('user_secrets', $id)", {"id": row_id})


async def delete_user_rows(db: AsyncSurreal, user_id: str) -> None:
    """Hard-delete a user's keys — no soft delete, see routes/users.delete_user."""
    await db.query("DELETE user_secrets WHERE user_id = $uid", {"uid": user_id})


# ─── MCP server personal keys (SYSTEM: mcp-servers) ──────────────────────────
# A row with server_id is this user's key for ONE mcp_servers row. The writers
# are the cabinet MCP routes only (a cookie session), same as the plain rows;
# the renderer (backend/mcp_servers.py) is the only plaintext reader.


async def server_key_row(db: AsyncSurreal, user_id: str, server_id: str) -> dict | None:
    """The user's key row for ONE server, or None — the (user, server) UNIQUE
    index's pair."""
    rows = await db.query(
        "SELECT * FROM user_secrets WHERE user_id = $uid AND server_id = $sid LIMIT 1",
        {"uid": user_id, "sid": server_id},
    )
    return rows[0] if rows else None


def _unique_violation(exc: Exception) -> bool:
    """SurrealDB's wording ("Database index `…` already contains […]"), both
    markers, not bare "index": a foreign error that happens to mention an
    index (e.g. "index file locked") must propagate — same classifier as
    create_row."""
    msg = str(exc).lower()
    return "index" in msg and "already contains" in msg


async def put_server_key(
    db: AsyncSurreal, user_id: str, *, server_id: str,
    host: str, port: int | None, value: str,
) -> None:
    """Upsert ONE server key row. The name is backend-minted `mcp--{server_id}`
    — server_id-derived, never from the server's name, so a rename cannot
    collide (validate_name is never called on it: its `--` refusal keeps
    minted names apart from user-chosen ones), and `hosts` is the server's
    CURRENT host at save time, so a later url change that moves the host makes
    the row count as not set ("re-enter key") instead of silently sending the
    key to the new host.
    """
    sets = {
        "ciphertext": encrypt_value(value),
        "hosts": [host_key(host, port)],
        "enabled": True,
    }
    existing = await server_key_row(db, user_id, server_id)
    if existing:
        await update_row(db, extract_id(existing["id"]), sets)
        return
    data = {
        "user_id": user_id,
        "name": f"mcp--{server_id}",
        "description": "",
        "header_name": DEFAULT_HEADER_NAME,
        "header_template": DEFAULT_HEADER_TEMPLATE,
        "server_id": server_id,
        **sets,
    }
    try:
        await db.query(
            "CREATE type::record('user_secrets', $id) CONTENT $data",
            {"id": uuid.uuid4().hex, "data": data},
        )
    except Exception as exc:  # noqa: BLE001 — a concurrent PUT lost the UNIQUE race
        if not _unique_violation(exc):
            raise
        lost = await server_key_row(db, user_id, server_id)
        if not lost:
            # INVARIANT(persisted): a UNIQUE violation with no row to update
            # is never swallowed.
            # Why: "Key saved" must not lie while nothing was stored.
            raise RuntimeError(
                f"user_secrets UNIQUE violation for ({user_id}, {server_id}) "
                "left no row to update",
            ) from exc
        await update_row(db, extract_id(lost["id"]), sets)


async def delete_server_rows(db: AsyncSurreal, user_id: str, server_id: str) -> None:
    """The user's rows for ONE server (cabinet DELETE — there is no soft delete)."""
    await db.query(
        "DELETE user_secrets WHERE user_id = $uid AND server_id = $sid",
        {"uid": user_id, "sid": server_id},
    )


async def delete_all_server_rows(db: AsyncSurreal, server_id: str) -> None:
    """Every user's rows for ONE server — the admin server-DELETE cascade and
    the personal→shared mode change (an orphaned ciphertext row is a
    credential left in the database)."""
    await db.query(
        "DELETE user_secrets WHERE server_id = $sid", {"sid": server_id},
    )
