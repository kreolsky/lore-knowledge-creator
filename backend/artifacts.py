"""Artifact core — the kind, the caps, origin config, pinned libraries, signed URLs (no routes).

# SYSTEM: artifact-serve — the cookieless-origin seam for agent-written html
# pages, SPLIT from the router: this module holds every
# piece the agent/tool layers need (the artifact kind predicate + media-type
# constant + content gate, the size cap and the create gates, the
# create-document description sentence rendered from the pinned manifest,
# token mint/verify, the frame-ancestors config, the boot-time host-split
# guard), while routes/artifact_serve.py owns the
# HTTP surface (/a/*, /lib/*, the two artifact-url mint routes, the
# host-split middleware). NON-route modules import HERE, never the router
# (import-shape gate: routes.* is route-only).

# ARCH: the artifact origin is a SEPARATE REGISTRABLE DOMAIN (config
# ARTIFACT_ORIGIN), never a subdomain of the app host — a page on
# art.<host> could set cookies on .<host> (cookie tossing / session
# fixation) and is same-site for the samesite="lax" session cookie on
# top-level GETs. check_artifact_origin_config refuses a sibling subdomain
# at boot for exactly that reason.

# INVARIANT(security): an artifact URL grants content for ≤10 minutes and
# nothing else. Why: the artifact host is cookieless, so /a/<id> cannot
# re-prove the caller — the mint routes own access (project read / the
# public share funnel) and the token bounds every URL's life, making a
# revocation take effect for an open frame at expiry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from settings_registry import ConfigError

import config
from config import SECRET_KEY


def artifact_origin() -> str:
    """The configured artifact origin, read at CALL time through config.

    Same stance as the SANDBOX/COMFYUI gates (agent.tools._gate_on): the key
    is declared effect=restart (boot-frozen for the admin surface), and the
    one sanctioned call-time reader is settings/config getattr — which also
    gives tests the single config-base pin (`pin_artifact_origin` in
    tests/helpers.py), mirroring pin_tool_gates.
    """
    return getattr(config, "ARTIFACT_ORIGIN", "")


#: The persisted artifact kind. ONE owner for the literal: the predicate,
#: the SQL gate below, and every writer's assignment read it from here. The
#: agent tool contract keeps its own `node_type="html"` literal
#: (backend/models/tools.py) — that names the TOOL argument, not the stored
#: kind; the i18n keys and user-visible strings are untouched.
ARTIFACT_MEDIA_TYPE = "html"


def is_artifact_row(row: dict | None) -> bool:
    """Whether `row` is an html artifact (a missing media_type = plain doc)."""
    return bool(row) and (row.get("media_type") or "") == ARTIFACT_MEDIA_TYPE


# The html-artifact content gate: an html row's content
# is page source — it is found by its TITLE alone, never by its markup. Every
# content predicate (agent/search_exec.py, routes/projects.py) reuses THIS
# fragment so a future arm cannot forget it. Verified on surreal v3.3.5 (gray,
# 2026-10-30): `NONE != 'html'` evaluates true, so plain rows (media_type
# NONE) pass untouched.
ARTIFACT_CONTENT_GATE = f"(media_type != '{ARTIFACT_MEDIA_TYPE}')"

#: Infra wiring, not a setting: both
#: Dockerfiles fetch the pinned libraries into this path at build time. It
#: lives OUTSIDE /app because dev/gray bind-mount ./backend over /app —
#: anything a build writes under /app is shadowed by the host tree and /lib/*
#: would 404 on gray while working in prod. MUST equal the literal in
#: backend/artifact_lib/fetch.py (the build stage imports no app code).
ARTIFACT_LIB_ROOT = Path("/opt/artifact_lib")

#: Signed-URL lifetime, seconds — the revocation window (see INVARIANT above).
ARTIFACT_URL_TTL_S = 600

#: Refusal ceiling for ONE html body, UTF-8 bytes — ONE limit for every
#: writer: agent create, import_file, edit growth, the human Source view.
#: Read-side cost is bounded elsewhere: read_document pages by
#: offset/limit (default window AGENT_READ_SLICE_CHARS, config.py), so a
#: page is never returned whole.
ARTIFACT_MAX_HTML_BYTES = 2 * 1024 * 1024


def _cap_label() -> str:
    """The cap in human units — a whole MiB renders MiB (2 MiB, not 2048 KiB).

    Read at call time so a test-pinned cap labels its own refusal honestly.
    """
    if ARTIFACT_MAX_HTML_BYTES % (1024 * 1024) == 0:
        return f"{ARTIFACT_MAX_HTML_BYTES // (1024 * 1024)} MiB"
    return f"{ARTIFACT_MAX_HTML_BYTES // 1024} KiB"


def assert_artifact_size(n_bytes: int) -> None:
    """422 when an html write would exceed the cap (create, import, edit growth).

    `n_bytes` is UTF-8 byte length — the cap is a byte ceiling, and a char
    count would let a Cyrillic page grow to ~2x the limit. Every caller
    reaches the constant through this module at call time, so ONE
    `monkeypatch.setattr(artifacts, "ARTIFACT_MAX_HTML_BYTES", …)` pins
    every writer in a test.
    """
    if n_bytes > ARTIFACT_MAX_HTML_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"html artifact exceeds the {_cap_label()} cap — shrink "
                   "the page before writing further",
        )


def assert_artifact_creatable(parent_id: str | None, is_reference: bool) -> None:
    """The artifact create gates, ONE wording for every entry point.

    Hosting must be configured (an unset ARTIFACT_ORIGIN deploy serves no
    pages), the page needs a parent (it is a leaf), and it is never a
    reference. Called at the tool BOUNDARY — before the apply-mode
    resolution — so a confirm-mode call is refused here instead of being
    parked on the mid-turn approval card and retried into a create that
    could never apply.
    """
    if not artifact_enabled():
        raise HTTPException(
            status_code=422,
            detail="artifact hosting is not configured — html pages "
                   "cannot be created on this instance",
        )
    if is_reference:
        raise HTTPException(
            status_code=422,
            detail="an html page is a leaf document, not a reference — "
                   "omit node_type and pass parent_id",
        )
    if not parent_id:
        raise HTTPException(
            status_code=422,
            detail="an html page is a leaf — pass parent_id (the document "
                   "it goes under)",
        )

_MANIFEST_PATH = Path(__file__).resolve().parent / "artifact_lib" / "manifest.json"
_manifest_cache: dict | None = None


def artifact_enabled() -> bool:
    """Whether artifact hosting is configured (ARTIFACT_ORIGIN set)."""
    return bool(artifact_origin())


def _manifest() -> dict:
    """The pinned library manifest (backend/artifact_lib/manifest.json).

    Cached at first read. A missing/corrupt manifest RAISES — it ships in the
    image, so its absence is a broken build, never "no libraries".
    """
    global _manifest_cache
    if _manifest_cache is None:
        _manifest_cache = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return _manifest_cache


def lib_entry(name_version: str) -> dict | None:
    """The manifest entry whose `<name>@<version>` matches, or None.

    The one sanctioned manifest walk for /lib/* serving — the router never
    touches `_manifest()` itself.
    """
    for lib in _manifest().get("libs", []):
        if f"{lib['name']}@{lib['version']}" == name_version:
            return lib
    return None


def artifact_lib_urls() -> list[str]:
    """Served library URLs (/lib/<name>@<version>/<file>), manifest-derived."""
    if not artifact_enabled():
        return []
    origin = artifact_origin()
    urls = []
    for lib in _manifest().get("libs", []):
        for f in lib.get("files", []):
            urls.append(f"{origin}/lib/{lib['name']}@{lib['version']}/{f['path']}")
    return urls


def html_node_sentence() -> str:
    """The html usage sentence for the create_document description.

    Rendered ONLY when artifact hosting is configured — an unset
    ARTIFACT_ORIGIN deploy serves a description that never mentions html
    (the handler's 422 names the reason if a caller still sends it). The
    library list is rendered from the pinned manifest, so the description
    and /lib/* cannot drift.
    """
    if not artifact_enabled():
        return ""
    libs = artifact_lib_urls()
    lib_phrase = f" (pinned libraries: {', '.join(libs)})" if libs else ""
    return (
        ' `node_type`="html" creates an interactive HTML page as a LEAF under '
        "parent_id: ONE self-contained file with its data inline, rendered in "
        f"a sandboxed frame with no network access{lib_phrase}. The page "
        "source rides `content` — or, for a page you build in the sandbox, "
        "import the .html file with import_file instead of re-sending it; "
        "edit it with edit_document str_replace; a page is refused past "
        f"{_cap_label()}. A tool call's JSON body is capped at 1 MiB, so a "
        "page over 1 MiB is built in the sandbox and imported with "
        "import_file. "
        'Declare the page\'s design in its head: `<meta name="color-scheme" '
        'content="dark">` for a dark design, `content="light"` for a light '
        "one — the viewer shows the page in that theme."
    )


def html_import_sentence() -> str:
    """The html sentence for the import_file description (hosting on only)."""
    if not artifact_enabled():
        return ""
    return (
        " A `.html`/`.htm` file becomes an interactive page leaf under "
        "parent_id (the same page create_document node_type=\"html\" makes) — "
        "the natural route for a page you built and checked in the sandbox."
    )


# ─── Signed URLs ──────────────────────────────────────────────────────────────


def _signing_key() -> bytes:
    """HMAC key derived from the per-install JWT secret (domain-separated)."""
    return hmac.new(SECRET_KEY.encode("utf-8"), b"artifact-url", hashlib.sha256).digest()


def mint_artifact_url(doc_id: str) -> str:
    """`<ARTIFACT_ORIGIN>/a/<id>?t=<exp>.<hmac>` — valid for ARTIFACT_URL_TTL_S."""
    exp = int(time.time()) + ARTIFACT_URL_TTL_S
    sig = hmac.new(_signing_key(), f"{doc_id}:{exp}".encode("ascii"), hashlib.sha256).hexdigest()
    return f"{artifact_origin()}/a/{doc_id}?t={exp}.{sig}"


def verify_artifact_token(doc_id: str, token: str) -> bool:
    """Constant-time check that the token is ours, for this doc, unexpired."""
    try:
        exp_raw, sig = token.split(".", 1)
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(
        _signing_key(), f"{doc_id}:{exp}".encode("ascii"), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


# ─── Host split ───────────────────────────────────────────────────────────────


def is_artifact_path(path: str) -> bool:
    """The artifact-host namespace: /a/* (pages) and /lib/* (pinned libraries)."""
    return path.startswith("/a/") or path.startswith("/lib/")


def on_artifact_host(host_header: str) -> bool:
    """Whether a request's Host names the artifact origin.

    When the origin carries an explicit port the Host must carry it too
    (dev/test); otherwise hosts compare without their ports (nginx forwards
    `Host $host`, and cookies are not port-scoped — same rule as
    main._same_host).
    """
    origin = artifact_origin()
    if not origin:
        return False
    parts = urlsplit(origin)
    host = host_header.strip().lower()
    if ":" in parts.netloc:
        return host == parts.netloc.lower()
    return host.rsplit(":", 1)[0].strip("[]") == (parts.hostname or "").lower()


def _last_two_labels(host: str) -> str:
    """The last two dot-labels of a host, lowercased (example.com)."""
    labels = host.lower().rstrip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def _looks_like_ip(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _hosts_conflict(artifact_host: str, app_host: str) -> bool:
    """Whether the artifact host is the app host or a dot-suffix sibling.

    IPs and `localhost` compare by exact host (their "last two labels" are
    meaningless); everything else compares its last two labels — a
    registrable-domain approximation that refuses art.example.com against
    app.example.com.
    """
    a, b = artifact_host.lower(), app_host.lower()
    if a == b:
        return True
    if (a == "localhost" or b == "localhost") or _looks_like_ip(a) or _looks_like_ip(b):
        return a == b
    return _last_two_labels(a) == _last_two_labels(b)


def check_artifact_origin_config(
    cors_origins: list[str], cors_env_set: bool, origin_value: str | None = None,
) -> None:
    """Boot-frozen validation of the artifact/app host split.

    Run by configure_artifact_host once the CORS origin list is resolved
    (`origin_value` defaults to the live config read so tests drive the
    branches without a process env). Raises ConfigError (refuses startup) when:
      - ARTIFACT_ORIGIN is set but CORS_ORIGINS is not — the localhost dev
        fallback would become `frame-ancestors`, blanking every frame in
        prod, and _same_host cannot backstop it (on /a/* the request host is
        the artifact host, never an app origin);
      - the artifact host equals or is a dot-suffix sibling of any CORS
        origin host — the cookie-tossing / same-site coupling the separate
        registrable domain exists to prevent.
    """
    origin = origin_value if origin_value is not None else artifact_origin()
    if not origin:
        return
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            f"ARTIFACT_ORIGIN={origin!r} is not an http(s) origin"
        )
    if not cors_origins or not cors_env_set:
        raise ConfigError(
            "ARTIFACT_ORIGIN is set but CORS_ORIGINS is not — an explicit "
            "CORS_ORIGINS is required when artifacts are served (the "
            "localhost fallback would become frame-ancestors and blank "
            "every artifact frame)"
        )
    artifact_host = parts.hostname
    for origin_entry in cors_origins:
        app_host = urlsplit(origin_entry).hostname or ""
        if app_host and _hosts_conflict(artifact_host, app_host):
            raise ConfigError(
                f"ARTIFACT_ORIGIN host {artifact_host!r} equals or is a "
                f"subdomain of the app origin {origin_entry!r} — artifacts must be "
                "served from a SEPARATE registrable domain (cookie tossing / "
                "same-site session coupling)"
            )


_frame_ancestors: list[str] | None = None


def configure_artifact_host(cors_origins: list[str], *, cors_env_set: bool) -> None:
    """Boot wiring (main.py): validate the host split, then store the list.

    frame-ancestors must name every origin a browser reaches the app from,
    or the embedded frame blanks — the SAME env var, the SAME dev fallback
    main.py already resolved for CORSMiddleware (one owner; the router never
    re-derives it). Runs the unchanged check_artifact_origin_config first,
    so an invalid split still refuses startup.
    """
    global _frame_ancestors
    check_artifact_origin_config(cors_origins, cors_env_set=cors_env_set)
    _frame_ancestors = list(cors_origins)


def frame_ancestors() -> list[str]:
    """The stored frame-ancestors list; RAISES if read before boot wiring.

    A None list means configure_artifact_host never ran (a non-main entry
    point importing the router cold) — serving /a/* with a guessed
    frame-ancestors would either blank every frame or open it to the web,
    so the misconfiguration is loud.
    """
    if _frame_ancestors is None:
        raise RuntimeError(
            "frame-ancestors requested before configure_artifact_host — "
            "main.py must wire the artifact host at boot"
        )
    return list(_frame_ancestors)
