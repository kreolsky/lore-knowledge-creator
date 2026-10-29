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
