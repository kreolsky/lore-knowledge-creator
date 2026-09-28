"""Compaction continuation minting from the driver's `compaction/end` dsh
event (plan pi-agent-layer-collapse step 5; the archived freeze row is deleted
by plan collapse-agent-stack-onto-dsh-vocabulary step 5 — the continuation is
the one row minted). Step 3 (lore-renders-dsh-conversation) rekeyed the relay
arm to the verbatim dsh frame: the mint keys the window identity pair the
module owns, and the OUTCOME rides beside the frame as `lore/compaction-mint`.
A turn that dies before the event mints NOTHING (no half-minted window).
Idempotent per compaction WINDOW (source + checkpoint entry id).
"""
from unittest.mock import AsyncMock

import pytest

# ─── Focused fake: the queries the mint issues ────────────────────────────────


class _FakeDB:
    """Handles the continuation window lookup (compacted_from + leaf)."""

    def __init__(self, chats: dict[str, dict] | None = None):
        self.chats = chats or {}
        self.queries: list[str] = []

    async def query(self, sql, params=None, **_kw):
        params = params or {}
        self.queries.append(sql)
        if "compacted_from = $src" in sql:
            src, leaf = params.get("src"), params.get("leaf")
            return [
                {"cid": cid} for cid, c in self.chats.items()
                if c.get("compacted_from") == src
                and ("compacted_at_leaf = $leaf" not in sql
                     or c.get("compacted_at_leaf") == leaf)
            ]
        return []


@pytest.fixture
def mint_env(monkeypatch):
    """Fake db + records for the mint module."""
    import driver.compaction as mod

    fake = _FakeDB()
    created: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(mod, "get_db", AsyncMock(return_value=fake))
    store: dict[str, dict] = fake.chats

    async def fake_create(table, rid, data):
        created.append((table, rid, data))
        # Record into the fake store so the window-guard queries can see the
        # rows minted earlier in the SAME test (idempotency replay).
        store[rid] = dict(data)

    monkeypatch.setattr(mod, "create_record", AsyncMock(side_effect=fake_create))
    monkeypatch.setattr(mod, "fetch_one", AsyncMock(
        side_effect=lambda table, rid: fake.chats.get(rid)))
    return {"db": fake, "created": created, "chats": fake.chats}


def _event(**over) -> dict:
    # compaction_entry_id: the stable window key the plugin's map guarantees
    # (the checkpoint's own seq id) — see window_leaf's INVARIANT(persisted).
    return {
        "type": "compaction", "session_id": "s1", "fork_id": "f-1",
        "compaction_entry_id": "e7",
        "project_id": "p1", "user_id": "u1", "document_id": None,
        "tokens_before": 9000, **over,
    }


def _dsh_compaction_end() -> dict:
    """The verbatim dsh frame the relay's compaction arm consumes (step-3
    vocabulary; shapes pinned by test_driver_frames.py)."""
    return {"type": "dsh_event", "kind": "compaction/end", "seq": 7,
            "data": {"compactionId": "e7", "turn": 1}}


@pytest.mark.asyncio
async def test_mint_mints_the_continuation_row(mint_env):
    """The event carries the fork identity; the backend mints the continuation
    chat row (same transcript continuing past the compaction checkpoint)."""
    import driver.compaction as mod

    mint_env["chats"]["s1"] = {"project_id": "p1", "user_id": "u1",
                               "document_id": "d1", "model": "m"}

    out = await mod.mint_compaction_chats(_event())

    assert out["created_continuation"] is True
    by_id = {rid: data for _t, rid, data in mint_env["created"]}
    cont_id = out["continuation_chat_id"]
    assert by_id[cont_id]["compacted_from"] == "s1"
    assert by_id[cont_id]["compacted_at_leaf"] == "e7"
    # The event's document_id (None) does NOT override — the source chat's
    # context layer inherits (matches the pre-SSE ArchiveIn semantics).
    assert by_id[cont_id]["document_id"] == "d1"
    assert by_id[cont_id]["model"] == "m"  # context inherited from the source chat


@pytest.mark.asyncio
async def test_mint_mints_no_archive_row(mint_env):
    """The archived freeze row is deleted with the freeze subsystem — only the
    continuation is minted."""
    import driver.compaction as mod

    mint_env["chats"]["s1"] = {"project_id": "p1", "user_id": "u1"}
    out = await mod.mint_compaction_chats(_event())
    assert "archived_chat_id" not in out
    assert "created_archive" not in out
    rows = [data for _t, _rid, data in mint_env["created"]]
    assert all("archived" not in r for r in rows), rows


@pytest.mark.asyncio
async def test_mint_is_idempotent_on_replay(mint_env):
    """A replayed event (same window) mints nothing new — the window key is
    source + leaf, so the guard returns the existing row verbatim."""
    import driver.compaction as mod

    mint_env["chats"]["s1"] = {"project_id": "p1", "user_id": "u1"}
    await mod.mint_compaction_chats(_event())
    first = list(mint_env["created"])

    out = await mod.mint_compaction_chats(_event())

    assert out["created_continuation"] is False
    assert mint_env["created"] == first


@pytest.mark.asyncio
async def test_second_window_mints_its_own_rows(mint_env):
    """A LATER compaction (different checkpoint) is a new window — its own
    continuation row, not a replay."""
    import driver.compaction as mod

    mint_env["chats"]["s1"] = {"project_id": "p1", "user_id": "u1"}
    await mod.mint_compaction_chats(_event())
    out = await mod.mint_compaction_chats(_event(compaction_entry_id="e12"))

    assert out["created_continuation"] is True


@pytest.mark.asyncio
async def test_missing_source_chat_mints_nothing(mint_env):
    """No source chat row (the session never had a turn server-side) → nothing
    to inherit from and nothing minted — logged, not an error."""
    import driver.compaction as mod

    out = await mod.mint_compaction_chats(_event())
    assert out["minted"] is False
    assert out["reason"] == "source_chat_missing"


@pytest.mark.asyncio
async def test_event_missing_fields_mints_nothing(mint_env):
    import driver.compaction as mod

    out = await mod.mint_compaction_chats({"type": "compaction"})
    assert out["reason"] == "missing_fields"
    assert mint_env["created"] == []


@pytest.mark.asyncio
async def test_relay_compaction_event_mints_and_relays(mint_env, monkeypatch):
    """The relay's compaction arm mints from the verbatim `compaction/end` dsh
    event (best-effort — a mint failure never breaks the turn) and relays the
    frame + the `lore/compaction-mint` outcome card beside it. The mint keys
    the window identity pair the module owns: the relay session plus the
    compaction id (dsh compacts in-place — the "fork" IS the window key)."""
    import driver.client
    import driver.compaction as compaction

    mint_env["chats"]["s1"] = {"project_id": "p1", "user_id": "u1"}
    called: list[dict] = []

    async def fake_mint(payload):
        called.append(payload)
        return {"continuation_chat_id": "cont-1", "created_continuation": True}

    monkeypatch.setattr(compaction, "mint_compaction_chats", fake_mint)
    r = driver.frames._TurnProjection(
        assistant_msg_id="m1", session_id="s1",
        persist_content=AsyncMock(), persist_sources=AsyncMock(),
        persist_extras=AsyncMock(), persist_turn_seq=AsyncMock(),
    )
    # The turn coordinate is the OPEN turn (turn/start), not the compaction
    # frame's payload — the same rule the live stream and the pin follow.
    await driver.frames._relay_frame(r, {"type": "dsh_event", "kind": "turn/start",
                                         "seq": 1, "data": {"turn": 1}})
    frames = await driver.frames._relay_frame(r, _dsh_compaction_end())
    assert called and called[0]["fork_id"] == "s1~ce7"
    parsed = frames  # the relay arms yield frame DICTS (the SSE skin died)
    assert [f["type"] for f in parsed] == ["dsh_event", "lore/compaction-mint"]
    assert parsed[1]["seq"] == 7.8
    assert parsed[1]["data"] == {"turn": 1, "compactionEntryId": "e7", "mintFailed": False}


@pytest.mark.asyncio
async def test_relay_mint_failure_still_relays(mint_env, monkeypatch):
    """Best-effort mint: a failure must not break the turn — the frame relays
    and the outcome card REPORTS the failure (mintFailed), never swallows it."""
    import driver.client
    import driver.compaction as compaction

    async def boom(_payload):
        raise RuntimeError("db down")

    monkeypatch.setattr(compaction, "mint_compaction_chats", boom)
    r = driver.frames._TurnProjection(
        assistant_msg_id="m1", session_id="s1",
        persist_content=AsyncMock(), persist_sources=AsyncMock(),
        persist_extras=AsyncMock(), persist_turn_seq=AsyncMock(),
    )
    frames = await driver.frames._relay_frame(r, _dsh_compaction_end())
    parsed = frames  # the relay arms yield frame DICTS (the SSE skin died)
    assert [f["type"] for f in parsed] == ["dsh_event", "lore/compaction-mint"]
    assert parsed[1]["data"]["mintFailed"] is True
    assert "db down" in parsed[1]["data"]["mintReason"]
