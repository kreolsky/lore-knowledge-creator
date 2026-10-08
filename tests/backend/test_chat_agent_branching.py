"""Agent chat branching via user-message edit — the single backend seam.

Seam A (`_sync_leaf_to_branch_point`) moves the driver session leaf to the
fork point BEFORE a turn, inside the setup try (a refusal is a real
pre-response 422, never an accepted turn that dies mid-flight). The branch
point is a DSH LOG SEQ — the driver's own id, stamped on the parent row when
its terminal frame relayed (`messages.driver_seq`; plan
collapse-the-editor-harness-layer step 5 deleted the Lore ordinal — the
ancestor-chain assistant count the plugin used to translate back into a seq).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import driver.channel
import pytest
import routes.chat.completions as comp
import routes.chat.completions_branch as branch  # seam bodies live here (mechanical split)
from fastapi import HTTPException
from helpers import pin_chat_api
from test_driver_channel import FakeConnector, FakeReplay, _env, _turn_end
from test_harness_turn import FollowupFake, _acquirable, _reset_fanout, _until_async


def _LINE():
    from driver.client import DriverLine
    return DriverLine(name="harness", url="http://harness.test", secret="s")


# ─── Seam A unit: _sync_leaf_to_branch_point ─────────────────────────────────


async def test_sync_leaf_sends_the_parent_row_driver_seq(monkeypatch):
    """Uniform: the leaf moves to the parent's TURN boundary (a DRIVER-side
    /session-leaf call — the transcript is driver-owned). A linear
    continuation names the current tail turn (a driver-side no-op); a fork
    names an earlier turn — one mechanism, no special-casing. The id is the
    row's OWN stamped seq: one fetch, no chain counting."""
    captured: list[tuple] = []

    async def fake_set(sid, seq, line=None, source=None):
        captured.append((sid, seq))

    rows = {"a2": {"role": "assistant", "parent_id": "u2", "driver_seq": 9}}
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(
        side_effect=lambda _t, mid: rows.get(mid)))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", fake_set)

    await comp._sync_leaf_to_branch_point(None, "chat", "sess", "a2")

    assert captured == [("sess", 9)]


async def test_sync_leaf_walks_a_user_parent_to_its_turn(monkeypatch):
    """A user row never ends a turn (no seq of its own) — the branch point is
    the nearest ANCESTOR row's seq. This is the retry-after-failed-turn shape:
    the failed turn's empty assistant row was deleted, the active tail is the
    user row, and the turn must continue the session at the last boundary."""
    captured: list[tuple] = []

    async def fake_set(sid, seq, line=None, source=None):
        captured.append((sid, seq))

    rows = {
        "u2": {"role": "user", "parent_id": "a1"},
        "a1": {"role": "assistant", "parent_id": "u1", "driver_seq": 5},
        "u1": {"role": "user", "parent_id": None},
    }
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(
        side_effect=lambda _t, mid: rows.get(mid)))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", fake_set)

    await comp._sync_leaf_to_branch_point(None, "chat", "sess", "u2")

    assert captured == [("sess", 5)]


async def test_sync_leaf_forks_at_last_completed_turn_under_an_unended_row(monkeypatch):
    """An abnormally ended turn wrote no `turn/end`, so its row carries no seq
    — the walk names the last COMPLETED boundary (an honest fork from there;
    the aborted partials are not model-canonical). Today's ordinal count
    422'd here; the seq walk resolves what the log actually holds."""
    captured: list[tuple] = []

    async def fake_set(sid, seq, line=None, source=None):
        captured.append((sid, seq))

    rows = {
        "a3": {"role": "assistant", "parent_id": "u3"},  # timed out mid-turn
        "u3": {"role": "user", "parent_id": "a2"},
        "a2": {"role": "assistant", "parent_id": "u2", "driver_seq": 9},
        "u2": {"role": "user", "parent_id": "u1"},
        "u1": {"role": "user", "parent_id": None},
    }
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(
        side_effect=lambda _t, mid: rows.get(mid)))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", fake_set)

    await comp._sync_leaf_to_branch_point(None, "chat", "sess", "a3")

    assert captured == [("sess", 9)]


async def test_sync_leaf_roots_a_chain_that_never_ran_a_turn(monkeypatch):
    """A chain of user rows only (the first completion after /messages, an
    edit+resend of the first message's child): no turn ever completed, so the
    branch is the ROOT — sent as the null seq (the driver resets to root)."""
    captured: list[tuple] = []

    async def fake_set(sid, seq, line=None, source=None):
        captured.append((sid, seq))

    rows = {"u1": {"role": "user", "parent_id": None}}
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(
        side_effect=lambda _t, mid: rows.get(mid)))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", fake_set)

    await comp._sync_leaf_to_branch_point(None, "chat", "sess", "u1")

    assert captured == [("sess", None)]


async def test_sync_leaf_refuses_when_no_row_carries_a_seq(monkeypatch):
    """Assistant rows the driver never accounted (a pre-harness thread: rows
    exist, no terminal frame ever stamped them) are NOT a root branch — a
    silent root reset would drop the history the UI shows above the fork.
    Honest 422, same as the ordinal era's driver-side refusal."""
    set_leaf = AsyncMock()
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(
        side_effect=lambda _t, mid: {
            "a1": {"role": "assistant", "parent_id": "u1"},
            "u1": {"role": "user", "parent_id": None},
        }.get(mid)))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", set_leaf)

    with pytest.raises(HTTPException) as exc:
        await comp._sync_leaf_to_branch_point(None, "chat", "sess", "a1")
    assert exc.value.status_code == 422
    set_leaf.assert_not_awaited()


async def test_sync_leaf_skips_when_no_parent_and_chat_empty(monkeypatch):
    """parent_id null with NO projection rows → genuine first message: no-op
    (the driver session starts fresh)."""
    set_leaf = AsyncMock()
    monkeypatch.setattr(branch, "_driver_set_session_leaf", set_leaf)
    monkeypatch.setattr(branch, "get_db", AsyncMock(return_value=_FakeCountDB(0)))

    await comp._sync_leaf_to_branch_point(None, "chat", "fresh", None)

    set_leaf.assert_not_awaited()


async def test_sync_leaf_resets_leaf_for_root_fork(monkeypatch):
    """Root fork: parent_id null (edit+resend of the FIRST message) on a chat
    whose projection HAS rows → the leaf is RESET to root so the driver
    appends the edited prompt as a ROOT sibling, not a linear child of the
    previous turn's answer (the corruption where the model saw every branch
    as one linear history)."""
    captured: list[tuple] = []

    async def fake_set(sid, seq, line=None, source=None):
        captured.append((sid, seq))

    monkeypatch.setattr(branch, "get_db", AsyncMock(return_value=_FakeCountDB(2)))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", fake_set)

    await comp._sync_leaf_to_branch_point(None, "chat", "sess", None)

    assert captured == [("sess", None)]  # seq null = root reset


async def test_sync_leaf_keeps_noop_for_continuation_root_send(monkeypatch):
    """Continuation chat (compacted_from set): a parent_id=null turn means
    "branch from the compaction summary" — the live leaf IS the compaction
    checkpoint and append-at-leaf already gives the right shape. The reset is
    skipped (resetting would replay pre-compaction history)."""
    set_leaf = AsyncMock()
    monkeypatch.setattr(branch, "_driver_set_session_leaf", set_leaf)

    await comp._sync_leaf_to_branch_point(
        None, "chat", "pi-root", None, is_continuation=True)

    set_leaf.assert_not_awaited()


async def test_sync_leaf_refuses_when_parent_deleted(monkeypatch):
    """A soft-deleted parent (fetch_one honours deleted_at → None breaks the
    chain) is not resolvable."""
    set_leaf = AsyncMock()
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(branch, "_driver_set_session_leaf", set_leaf)

    with pytest.raises(HTTPException) as exc:
        await comp._sync_leaf_to_branch_point(None, "chat", "sess", "gone")
    assert exc.value.status_code == 422
    set_leaf.assert_not_awaited()  # leaf move never reached on refusal


async def test_sync_leaf_refuses_when_chain_cycles(monkeypatch):
    """A corrupt tree (a parent cycle) is not a branch point — honest 422."""
    monkeypatch.setattr(branch, "fetch_one", AsyncMock(
        side_effect=lambda _t, mid: {"role": "user", "parent_id": "other"} if mid == "a"
        else {"role": "assistant", "parent_id": "a"}))
    set_leaf = AsyncMock()
    monkeypatch.setattr(branch, "_driver_set_session_leaf", set_leaf)

    with pytest.raises(HTTPException) as exc:
        await comp._sync_leaf_to_branch_point(None, "chat", "sess", "a")
    assert exc.value.status_code == 422


# ─── the branch point is the PAIR (dsh session, seq): the POSTed body ─────────


class _RecordingDriverClient:
    """The `driver` pool client: records every POST body, answers 200 with
    `reply` (default: the live-tail no-op's `{ok: true}`)."""

    def __init__(self, reply: dict | None = None):
        self.bodies: list[dict] = []
        self.reply = reply or {"ok": True}

    async def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)
        return SimpleNamespace(status_code=200, json=lambda: self.reply)


async def _mk_rows(test_db, rows: dict[str, dict]) -> None:
    """Real message rows — the walk reads them through fetch_one unpatched."""
    for mid, fields in rows.items():
        await test_db.query(
            "CREATE type::record('messages', $id) CONTENT $f",
            {"id": mid, "f": {"chat_id": "chat-pair", "content": "", **fields}},
        )


async def test_sync_leaf_posts_the_parent_rows_driver_session_as_source(
    test_db, http_pool,
):
    """The seq is resolvable only inside the dsh session that logged it (a
    root fork restarts seqs at 0) — the row's OWN driver_session rides beside
    it as `source`, taken from the same row that supplied the seq."""
    driver_client = http_pool("driver", _RecordingDriverClient())
    await _mk_rows(test_db, {
        "pair-a1": {"role": "assistant", "driver_seq": 5, "driver_session": "lore-chat"},
        "pair-u2": {"role": "user", "parent_id": "pair-a1"},
        "pair-a2": {"role": "assistant", "parent_id": "pair-u2", "driver_seq": 9,
                    "driver_session": "lore-chat~fabc12345"},
        "pair-u3": {"role": "user", "parent_id": "pair-a2"},
    })

    await comp._sync_leaf_to_branch_point(
        None, "chat-pair", "lore-chat", "pair-u3", line=_LINE())

    assert driver_client.bodies == [
        {"session_id": "lore-chat", "seq": 9, "source": "lore-chat~fabc12345"}]


async def test_sync_leaf_omits_source_for_a_legacy_row(test_db, http_pool):
    """A row stamped before the pair (driver_session NONE) sends no `source`:
    the driver resolves against its current session, as before."""
    driver_client = http_pool("driver", _RecordingDriverClient())
    await _mk_rows(test_db, {"legacy-a1": {"role": "assistant", "driver_seq": 5}})

    await comp._sync_leaf_to_branch_point(
        None, "chat-pair", "lore-chat", "legacy-a1", line=_LINE())

    assert driver_client.bodies == [{"session_id": "lore-chat", "seq": 5}]


async def test_a_fork_answer_repoints_the_live_subscription(http_pool):
    """The driver's re-ack rides the event socket, which a harness restart
    leaves down for seconds; the fork's HTTP answer carries the same repoint
    and seam A applies it to the process channel's subscription, so the
    forked turn reaches its row either way."""
    ch = driver.channel.get_driver_channel()
    sub = driver.channel._Subscription(
        lore_session_id="lore-chat", dsh_session_id="lore-chat", last_seq=100.0)
    ch._subs["lore-chat"] = sub
    try:
        http_pool("driver", _RecordingDriverClient(
            {"ok": True, "dsh_session_id": "lore-chat~f0000beef", "tail_seq": 9}))
        await branch._driver_set_session_leaf("lore-chat", 9, line=_LINE())
        assert (sub.dsh_session_id, sub.last_seq) == ("lore-chat~f0000beef", 9.0)
        assert ch._dsh_index.get("lore-chat~f0000beef") is sub

        # A no-op answer (no fork) leaves the subscription where it was.
        sub.last_seq = 12.0
        http_pool("driver", _RecordingDriverClient())
        await branch._driver_set_session_leaf("lore-chat", 9, line=_LINE())
        assert (sub.dsh_session_id, sub.last_seq) == ("lore-chat~f0000beef", 12.0)
    finally:
        ch._subs.pop("lore-chat", None)
        ch._dsh_index.pop("lore-chat~f0000beef", None)


# ─── Seam A placement: a real pre-response 422, not a dead-on-arrival turn ───


class _FakeCountDB:
    """Answers the root-fork projection count query."""

    def __init__(self, n: int):
        self.n = n

    async def query(self, _sql, _params=None, **_kw):
        return [{"n": self.n}]


@pytest.fixture
async def harness_env(monkeypatch):
    """test_harness_turn's harness_env with ONE divergence: seam A runs for
    real. The no-op patch the harness tests apply would hollow out the very
    placement these route tests pin — here the seam's driver RPC is faked
    per-test on the branch module instead."""
    import driver.timeline as tl

    connector = FakeConnector()
    replay = FakeReplay()
    followups = FollowupFake()
    async def _fake_line():
        return _LINE()

    monkeypatch.setattr(driver.channel, "resolve_driver_line", _fake_line)
    monkeypatch.setattr(driver.channel, "_ws_connect", connector)
    monkeypatch.setattr(driver.channel, "fetch_session_entries", replay)
    monkeypatch.setattr(driver.channel, "_RECONNECT_MIN_S", 0.01)
    monkeypatch.setattr(driver.channel, "_WATCHDOG_TICK_S", 0.01)
    monkeypatch.setattr(driver.channel, "_SUBSCRIBE_ACK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(tl, "post_followup", followups)
    pin_chat_api(monkeypatch)

    async def _ok_rate(*_a, **_k):
        return True

    monkeypatch.setattr(comp, "check_completion_rate_limit", _ok_rate)
    monkeypatch.setattr(comp, "resolve_driver_line", _fake_line)

    driver.channel._channel = None
    yield SimpleNamespace(
        connector=connector, replay=replay, followups=followups)
    await driver.channel.get_driver_channel().aclose()
    driver.channel._channel = None
    _reset_fanout()


async def _create_session(client, token, pid, doc_id):
    resp = await client.post(
        "/api/chat/sessions",
        json={"project_id": pid, "document_id": doc_id, "model": "test"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


async def _create_user_message(client, token, sid, content):
    resp = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        json={"content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["message_id"]


async def test_fork_from_deleted_parent_returns_real_422(
    client, admin_user, project_with_doc, harness_env,
):
    """The load-bearing placement: a fork whose parent row is deleted mid-chat
    returns a REAL 422 — seam A runs before the turn is accepted, so the
    refusal is a proper error response, not an accepted turn that dies
    mid-flight (which would violate no-silent-degradation)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    # A user message created via /messages then soft-deleted → broken chain.
    parent_id = await _create_user_message(client, token, sid, "original prompt")
    from db import get_db

    db = await get_db()
    await db.query(
        "UPDATE type::record('messages', $id) SET deleted_at = time::now()",
        {"id": parent_id},
    )

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={
            "messages": [{"role": "user", "content": "forked prompt"}],
            "parent_id": parent_id,
        },
        cookies={"lore_session": token},
    )

    # The hard constraint: a real 422, NOT an accepted turn.
    assert resp.status_code == 422, resp.text
    assert "resolvable" in resp.text.lower()


async def test_fork_from_live_parent_proceeds_to_the_turn(
    client, admin_user, project_with_doc, harness_env, monkeypatch,
):
    """A turn whose parent row is a live message does NOT 422 — the seam
    resolves a branch point (here: a user-only chain → the root branch) and
    the turn is handed to the driver (the seam works end to end against a
    faked harness)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    sid = await _create_session(client, token, pid, doc_id)
    parent_id = await _create_user_message(client, token, sid, "original prompt")
    env = harness_env

    captured: list = []

    async def fake_set_leaf(_sid, seq, line=None, source=None):
        captured.append(seq)

    monkeypatch.setattr(branch, "_driver_set_session_leaf", fake_set_leaf)

    resp = await client.post(
        f"/api/chat/sessions/{sid}/completions",
        json={
            "messages": [{"role": "user", "content": "continue"}],
            "parent_id": parent_id,
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True

    # The user-only chain resolved to the ROOT branch, then the turn was
    # handed off (the followup payload recorded — what the retired fake
    # stream's "ran" marker pinned).
    assert captured == [None]
    assert len(env.followups.payloads) == 1

    # Close the turn through the channel → the lock releases.
    sock = env.connector.sockets[0]
    for frame in ({"type": "model_update", "model": "test"}, _turn_end(1)):
        sock.push(_env(sid, frame))
    await _until_async(lambda: _acquirable(sid))
