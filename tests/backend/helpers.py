"""Test helper functions shared across backend test modules."""

import asyncio
import contextlib
import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from pycrdt import Doc, Text

from config import ALGORITHM, COOKIE_MAX_AGE, SECRET_KEY

# ─── Sync-loop driver for low-level collab tests ─────────────────────────────
# The collab TestClient drives the app on anyio's portal thread; the surgical
# CRDT tests drive `apply_external_content_change` on an INDEPENDENT loop from the
# main thread (the app's session objects are module-level and loop-agnostic).
# `asyncio.get_event_loop()` raises RuntimeError in Py3.12 once the main-thread
# loop has been set-then-cleared (the suite does this), so we own a dedicated
# loop here instead of relying on the deprecated implicit-creation behavior.
_run_sync_loop: asyncio.AbstractEventLoop | None = None


def run_sync(coro):
    """Run a coroutine to completion on a dedicated event loop (Py3.12-safe).

    Replaces `asyncio.get_event_loop().run_until_complete(coro)`, which is
    flaky/broken under Python 3.12 in the test main thread. The loop is created
    lazily and reused across calls, mirroring the historical semantics where
    `get_event_loop()` returned one cached main-thread loop for the whole run.
    """
    global _run_sync_loop
    if _run_sync_loop is None or _run_sync_loop.is_closed():
        _run_sync_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_run_sync_loop)
    return _run_sync_loop.run_until_complete(coro)


# ─── CRDT collab test helpers ────────────────────────────────────────────────
# The OT push wire protocol is gone; edits now ride the pycrdt Y.Doc. These
# helpers drive a CollabSession's document the way a real client would, so the
# behavioral tests (flush→DB, mentions, backup, degraded save) exercise the
# real flush pipeline without reconstructing the binary WS frames by hand.


def set_session_text(session, text: str) -> None:
    """Replace a collab session's derived Y.Text content (does NOT mark dirty)."""
    t = session._get_text()
    if len(t) > 0:
        del t[0:len(t)]
    if text:
        t += text


class _DummyWS:
    """No-op WebSocket stand-in for handle_binary_message's sender_ws."""
    async def send_bytes(self, data: bytes) -> None:
        pass

    async def send_text(self, data: str) -> None:
        pass


async def apply_binary_edit(session, text: str, at: int = 0) -> None:
    """Insert `text` into the session via the real binary Yjs path.

    Builds a diff update from a synced replica and feeds it through
    handle_binary_message (non-multiplexed), which sets _dirty and appends to
    the update log exactly as a live client edit would.
    """
    ws = _DummyWS()
    if id(ws) not in session.clients:
        session.add_client(ws, "test-user", "Test", "full")
    replica = Doc()
    replica.apply_update(session.ydoc.get_update())
    state_before = session.ydoc.get_state()
    replica.get("content", type=Text).insert(at, text)
    diff = replica.get_update(state_before)
    # MSG_SYNC=0, MSG_SYNC_UPDATE=2
    await session.handle_binary_message(bytes([0, 2]) + diff, ws, is_multiplexed=False)


def make_token(user_id: str, name: str, role: str = "user", email: str = "", token_version: int = 0) -> str:
    """Generate a JWT session token for test auth."""
    return jwt.encode(
        {
            "user_id": user_id, "name": name, "email": email, "role": role,
            "token_version": token_version,
            "exp": datetime.now(timezone.utc) + timedelta(seconds=COOKIE_MAX_AGE),
            "iat": datetime.now(timezone.utc),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


# ─── Project collab WS join helpers ──────────────────────────────────────────
# The per-entity collab channel (URL-borne entity type + id) is deleted (plan
# fewer-layers); the multiplexed project channel is the one collab surface.
# These two keep the connect+join dance uniform across the integration tests
# moved off the dead channel — the join ack (init) is consumed by the caller.


def project_collab_url(project_id: str) -> str:
    """URL of the multiplexed project collab WS (the one collab channel)."""
    return f"/ws/collab/project/{project_id}"


def join_collab_ws(ws, entity_id: str, entity_type: str = "doc") -> None:
    """Send the join control frame for one entity on the project collab WS."""
    ws.send_text(json.dumps({"type": "join", "entity_type": entity_type, "entity_id": entity_id}))


# ─── project_with_doc isolation: project_id-keyed child wipe ──────────────────
# Every table here carries a `project_id` field (see surreal/schema.surql) and is
# data a test can leave behind under the fixture's fixed pid. `documents` is
# listed first because it is the leak surface observed in CI run #1042 —
# references are documents (is_reference=true), so a leaked reference is a leaked
# document. `projects` itself is NOT here: it is the parent, deleted by id in the
# fixture. The order matches _DELETE_ALL_SQL's edges-first discipline (no cross-
# table FK events fire on these DELETEs, but keeping a stable, reviewable order).
PROJECT_CHILD_TABLES = (
    "documents",        # includes references (is_reference=true)
    "chat_sessions",
    "doc_chunks",
    "api_keys",
    "agent_configs",
    "pipeline_schedules",
    "document_shares",
    "pending_invites",
    "project_members",
    "user_preferences",
    "telemetry_event",
)


def wipe_project_children_sql() -> str:
    """One round-trip batched DELETE of every project_id-keyed child row for a pid.

    Used by the `project_with_doc` fixture setup to guarantee its "clean project"
    contract holds even when the prior test's `client`-teardown was silently
    swallowed — `_DELETE_ALL_SQL` in conftest is wrapped in `except Exception:
    pass`, and run #1042 flaked (`test_references_pagination` saw 8 references
    after POSTing 5) when that no-op'd and 3 reference-documents survived.

    Mirrors `_DELETE_ALL_SQL`'s single multi-statement pattern: 12 tables joined
    into one SurrealDB query instead of 12 sequential round-trips. Filtering by
    `project_id = $pid` (not a bare `DELETE`) keeps it scoped to the one pid, so
    the project row and unrelated rows in the same worker DB survive. The caller
    binds `$pid`.
    """
    return "; ".join(
        f"DELETE {t} WHERE project_id = $pid" for t in PROJECT_CHILD_TABLES
    )


def skill_frontmatter(content: str) -> dict:
    """Test-side frontmatter reader for a RAW skill document.

    Plan collapse-the-editor-harness-layer step 3: the production parse lives
    in the harness plugin (harness-driver/plugin/src/skills.ts) — Python parses
    nothing. Tests that pin shipped-skill PROSE (packs, descriptions, body
    defaults) still need the fields, so they read them HERE — test glue over a
    fixture, not a second production parser.

    Returns the frontmatter dict with `body` (frontmatter-stripped, stripped)
    added.
    """
    import yaml

    normalized = (content or "").replace("\r\n", "\n")
    assert normalized.startswith("---\n"), "not a frontmatter skill document"
    end = normalized.index("\n---", 4)
    meta = yaml.safe_load(normalized[4:end])
    assert isinstance(meta, dict) and meta.get("name"), "malformed skill frontmatter"
    meta["body"] = normalized[end + 4:].strip()
    return meta


# ─── Config fallback-chain pins (instance-settings step 2) ───────────────────
# Readers resolve the API lines through settings.get, which walks the registry
# fallback links of the keys config.py folds at import (STT ← AI; EMBEDDING ←
# AI). A patch on the dependent's config attr alone never wins while the
# base's env var is set — the walk returns the first key whose OWN env is
# set. Pinning every link on config (the settings fallback bucket) is
# deterministic in every environment: dev .env sets the base, CI sets it empty,
# a deploy may set any link of the chain.

_CHAT_API_LINKS = (
    "AI_API_URL", "STT_API_URL",
    "AI_API_KEY", "STT_API_KEY",
)


def pin_chat_api(monkeypatch, url: str = "http://fake", key: str = "k") -> None:
    """Pin the AI/STT URL/KEY links on config (the chat readers read AI_API_*)."""
    import config

    for name in _CHAT_API_LINKS:
        monkeypatch.setattr(config, name, url if name.endswith("URL") else key)


@contextlib.contextmanager
def pinned_chat_api(url: str = "http://fake", key: str = "k"):
    """Context-manager twin of pin_chat_api (for stacked `with patch(...)` sites)."""
    import config

    with contextlib.ExitStack() as stack:
        for name in _CHAT_API_LINKS:
            stack.enter_context(
                patch.object(config, name, url if name.endswith("URL") else key),
            )
        yield


def pin_stt_url(monkeypatch, url: str = "http://stt") -> None:
    """Pin the STT→AI URL chain on config (the transcription-configured gate)."""
    import config

    monkeypatch.setattr(config, "STT_API_URL", url)
    monkeypatch.setattr(config, "AI_API_URL", url)


@contextlib.contextmanager
def pinned_stt_url(url: str = "http://stt"):
    """Context-manager twin of pin_stt_url."""
    import config

    with patch.object(config, "STT_API_URL", url), \
         patch.object(config, "AI_API_URL", url):
        yield


def pin_embedding_api(
    monkeypatch, url: str = "http://embedding.test", key: str = "test-key",
) -> None:
    """Pin the EMBEDDING→AI URL/KEY chain on config."""
    import config

    monkeypatch.setattr(config, "EMBEDDING_API_URL", url)
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", key)
    monkeypatch.setattr(config, "AI_API_URL", url)
    monkeypatch.setattr(config, "AI_API_KEY", key)


@contextlib.contextmanager
def pinned_embedding_api(
    url: str = "http://embedding.test", key: str = "test-key",
):
    """Context-manager twin of pin_embedding_api."""
    import config

    with patch.object(config, "EMBEDDING_API_URL", url), \
         patch.object(config, "EMBEDDING_API_KEY", key), \
         patch.object(config, "AI_API_URL", url), \
         patch.object(config, "AI_API_KEY", key):
        yield


def pin_tool_gates(
    monkeypatch, *, sandbox: bool | None = None, comfy: bool | None = None,
) -> None:
    """Pin the agent tool gates via their config BASE keys.

    The served-tool gates (SANDBOX/COMFYUI _ENABLED) are config folds
    re-derived from the base keys at call time by agent.tools._gate_on (through
    settings), so a test pins a gate by pinning its bases — the same config.*
    fallback bucket the other pin_* helpers use.
    """
    import config

    if sandbox is not None:
        monkeypatch.setattr(config, "SANDBOX_SSH_HOST", "sandbox-test" if sandbox else "")
        monkeypatch.setattr(config, "SANDBOX_SSH_KEY_B64", "dGVzdC1rZXk=" if sandbox else "")
    if comfy is not None:
        monkeypatch.setattr(config, "COMFYUI_URL", "http://comfy-test" if comfy else "")


#: A small marked workflow: sampler 3 [lore:seed], prompt 6 [lore:prompt],
#: latent 88 [lore:size] [lore:batch] (linked width/height, like the shipped
#: graph), SaveImage 90.
COMFY_TEST_WORKFLOW: dict = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 5, "steps": 4},
          "_meta": {"title": "KSampler [lore:seed]"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "baked prompt"},
          "_meta": {"title": "Positive [lore:prompt]"}},
    "88": {"class_type": "EmptyLatentImage",
           "inputs": {"width": ["13", 1], "height": ["13", 2], "batch_size": 1},
           "_meta": {"title": "Latent [lore:size] [lore:batch]"}},
    "90": {"class_type": "SaveImage",
           "inputs": {"filename_prefix": "Krea2", "images": ["8", 0]},
           "_meta": {"title": "Save Image"}},
}


def pin_comfy_config(
    monkeypatch, *, workflow: dict | None = None, prompt: str = "TEMPLATE BODY",
) -> None:
    """Pin the instance Comfy admin settings (prompt, workflow, the three sizes)
    on the config.* bucket the settings fallback leg reads."""
    import json

    import config

    monkeypatch.setattr(config, "COMFYUI_PROMPT", prompt)
    monkeypatch.setattr(
        config, "COMFYUI_WORKFLOW",
        json.dumps(COMFY_TEST_WORKFLOW if workflow is None else workflow),
    )
    for orientation, size in (
        ("SQUARE", "1024x1024"), ("PORTRAIT", "832x1216"), ("LANDSCAPE", "1216x832"),
    ):
        monkeypatch.setattr(config, f"COMFYUI_SIZE_{orientation}", size)


def pin_comfy_prompt_model(monkeypatch, model: str = "test-refiner") -> None:
    """Pin the COMFYUI_PROMPT_MODEL ← CHAT_MODEL chain on config.

    Both links must be pinned: settings.get walks the registry fallback
    (row → OWN env → base), so a config.COMFYUI_PROMPT_MODEL patch alone loses
    to a set CHAT_MODEL env var — same reasoning as the chat-api chain twins
    above.
    """
    import config

    monkeypatch.setattr(config, "COMFYUI_PROMPT_MODEL", model)
    monkeypatch.setattr(config, "CHAT_MODEL", model)


@contextlib.contextmanager
def pinned_tool_gates(
    *, sandbox: bool | None = None, comfy: bool | None = None,
):
    """Context-manager twin of pin_tool_gates (for stacked `with patch(...)` sites)."""
    import config

    pins: list[tuple[str, object]] = []
    if sandbox is not None:
        pins += [
            ("SANDBOX_SSH_HOST", "sandbox-test" if sandbox else ""),
            ("SANDBOX_SSH_KEY_B64", "dGVzdC1rZXk=" if sandbox else ""),
        ]
    if comfy is not None:
        pins.append(("COMFYUI_URL", "http://comfy-test" if comfy else ""))
    with contextlib.ExitStack() as stack:
        for name, value in pins:
            stack.enter_context(patch.object(config, name, value))
        yield


# ─── Round-trip oracles ──────────────────────────────────────────────────────
# `parse_gfm_table` / `read_table_grid` / `derive_content` had no production
# caller: the GFM→model parse direction
# lives on the frontend (gfm-table-import.ts), production serializes only, and
# content derivation happens inside the write paths. They stay as ORACLES —
# tests read production-written state back through them to pin round-trips.


_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _unescape_cell(wire: str) -> str:
    """Reverse `_escape_cell`: `<br>` → newline, `\\|` → literal pipe.

    Matches the frontend ``gfm-table-import.ts`` unescape: any ``<br>``/``<br/>``/``<br />``,
    case-insensitive (GFM-spec lenient), so the two parsers agree byte-for-byte.
    """
    return re.sub(r"<br\s*/?>", "\n", wire, flags=re.IGNORECASE).replace("\\|", "|")


def _is_separator(line: str) -> bool:
    """True for a GFM header-separator row (`| --- | :--: | …`)."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [c.strip() for c in _CELL_SPLIT.split(stripped[1:-1])]
    # Require >=1 NON-EMPTY dash cell. Why: ``all([])`` is True, so without this an all-empty
    # data row (``|   |   |``) is misclassified as the separator and silently dropped on import.
    non_empty = [c for c in cells if c != ""]
    return bool(non_empty) and all(re.fullmatch(r":?-+:?", c) for c in non_empty)


def _parse_row(line: str) -> list[str]:
    inner = line.strip()[1:-1]  # drop leading/trailing pipe
    out: list[str] = []
    for seg in _CELL_SPLIT.split(inner):
        # Strip exactly the one padding space added on each side by serialize_table.
        if seg.startswith(" "):
            seg = seg[1:]
        if seg.endswith(" "):
            seg = seg[:-1]
        out.append(_unescape_cell(seg))
    return out


def parse_gfm_table(md: str) -> list[list[str]]:
    """Test oracle: parse a GFM table string into the cell-body model (the inverse
    of `serialize_table`). The separator row is dropped; every other non-empty
    `|`-delimited line is a data row. `<br>` → newline; `\\|` → literal pipe.
    """
    rows: list[list[str]] = []
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        if _is_separator(line):
            continue
        rows.append(_parse_row(line))
    return rows


def read_table_grid(doc, table_id: str) -> dict | None:
    """Test oracle: read one table as `{columns, rows}` (widths + flat cell-body
    matrix), ``None`` when the table id is absent. Reads through the SAME
    primitive as `capture_tables_json` (`_table_columns_and_rows`), so the
    oracle and the checkpoint capture cannot drift on shape.
    """
    from pycrdt import Map

    from table_serialize import _table_columns_and_rows

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return None
    widths, rows = _table_columns_and_rows(tables[table_id])
    return {"columns": widths, "rows": rows}


async def derive_content(entity_id: str) -> str:
    """Test oracle: plaintext content from the CRDT state (table anchors → GFM)."""
    from ydoc_store import expand_tables, load

    doc = await load(entity_id)
    return expand_tables(doc)
