"""Driver-contract tests: the relay arms fed FAKE frames (no live driver
container, no LLM gateway) against the REAL persistence (test_db).

Plan lore-renders-dsh-conversation step 3 rekeyed this file; plan
agent-line-harness-lifecycle step 9 removed the pump (`map_driver_events` is
deleted — every turn rides the standing channel), so the frame-driven tests
below advance the SAME projection through the SAME relay arms via
`_drive_turn` (a channel in miniature: no dedup, no deadline — the channel's
own machinery is pinned in test_driver_channel.py). The fakes speak ONLY the
verbatim `dsh_event` vocabulary (shapes pinned by test_driver_frames.py, the
source of truth). What remains here is what the reducer-family tests do not
cover: the REAL-DB persistence paths (the driver_seq stamp, the sources
panel, abnormal finalize), the turn payload, and agent_capability.

The per-kind translation cases (delta/reasoning/agent_step/turn_halted and the
per-target repeat bound) are deleted WITH the translator — the relay drops
nothing and derives nothing from a frame it can pass through. The pump-only
triggers (SSE stream death, the deadline breach, client-disconnect
GeneratorExit) died with the pump; their persist semantics live on in the
channel and are pinned there (breach, unsubscribe/disconnect).
"""

import json

import pytest
from driver.client import (
    _build_turn_payload,
)
from helpers import pin_chat_api

import config

# ─── dsh vocabulary builders (shapes pinned by test_driver_frames.py) ─────────


def _dsh(seq, kind, data=None, **extra):
    return {"type": "dsh_event", "kind": kind, "seq": seq, "data": data, **extra}


def _chunk(seq, text):
    # v3: the settled assistant/message (whole step text) is the content
    # source — assistant/chunk died with the old format.
    return _dsh(seq, "assistant/message", {
        "turn": 1, "step": 1,
        "message": {"id": f"m{seq}", "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "source": {"kind": "model"}},
        "stream": []}, surfaceOp="append")


def _turn_end(seq, kind="completed"):
    return _dsh(seq, "turn/end", {"turn": 1, "reason": {"kind": kind}})


def _applied_edit_result(call_id, doc="doc-x", seq=1):
    """One completed AS-mode (auto-apply) edit round-trip: the dispatching
    `tool/call` and its settled `tool/result` — the settled frame is what the
    finished-step count (the abnormal halt card's honest position) reads."""
    return [
        _dsh(seq, "tool/call", {
            "turn": 1, "step": 1, "callId": call_id, "name": "edit_document",
            "arguments": json.dumps(
                {"document_id": doc, "old_string": "a", "new_string": "b"})}),
        _dsh(seq + 1, "tool/result", {
            "turn": 1, "step": 1, "message": {
                "role": "tool", "source": {"kind": "tool", "callId": call_id},
                "toolCallId": call_id, "content": [{"type": "text", "text": "ok"}]}}),
    ]


async def _drive_turn(
    frames, *, assistant_msg_id: str, sources: list | None = None,
    session_id: str = "",
):
    """The pump's old loop body, post-pump: advance ONE projection over the
    frames through the REAL relay arms (REAL persist fns — test_db), stop at
    the turn's terminal frame, and close with the pump's finalize semantics
    (graceful finalize + the context stamp the arms captured). Returns
    (projection, emitted frames).

    This is a channel in miniature — no dedup, no deadline, no socket. The
    channel's own dispatch/breach/unsubscribe machinery is pinned in
    test_driver_channel.py; what THIS helper preserves is the arms +
    persistence against the real row."""
    from driver.frames import _relay_frame, _TurnProjection
    from driver.persistence import (
        _persist_content,
        _persist_context_usage,
        _persist_projection_extras,
        _persist_sources,
        _persist_turn_seq,
    )

    turn = _TurnProjection(
        assistant_msg_id=assistant_msg_id, sources=sources,
        persist_content=_persist_content, persist_sources=_persist_sources,
        persist_extras=_persist_projection_extras,
        persist_turn_seq=_persist_turn_seq, session_id=session_id,
    )
    emitted: list[dict] = []
    for frame in frames:
        emitted.extend(await _relay_frame(turn, frame))
        if turn.finished:
            break
    if turn.finalize_pending:
        await turn.finalize()
        if turn.context_usage and session_id:
            await _persist_context_usage(session_id, int(turn.context_usage["used"]))
    return turn, emitted


# ─── the driver_seq stamp, against the REAL row ───────────────────────────────


@pytest.mark.asyncio
async def test_turn_end_stamps_the_dsh_session_beside_the_seq(client, test_db, project_with_doc):
    """The branch point is the pair: `driver_session` lands on the row with
    `driver_seq`; a turn with no known dsh id writes NONE (the legacy path)."""
    from driver.frames import _relay_frame, _TurnProjection
    from driver.persistence import _persist_content, _persist_sources, _persist_turn_seq

    for msg_id, dsh in (("pi-msg-pair", "lore-x~f0badc0d"), ("pi-msg-pair-none", None)):
        await _mk_msg(test_db, msg_id)
        turn = _TurnProjection(
            assistant_msg_id=msg_id, persist_content=_persist_content,
            persist_sources=_persist_sources, persist_turn_seq=_persist_turn_seq,
            dsh_session_id=dsh,
        )
        for frame in (_chunk(1, "answer"), _turn_end(3)):
            await _relay_frame(turn, frame)
        await turn.finalize()
        row = await test_db.query(
            "SELECT driver_seq, driver_session FROM type::record('messages', $id)",
            {"id": msg_id})
        assert row[0]["driver_seq"] == 3
        assert row[0].get("driver_session") == dsh


# ─── Turn payload shape ───────────────────────────────────────────────────────


def test_build_turn_payload_carries_time_stamps():
    """The turn contract carries the stamp lines as their OWN field — `prompt`
    is the raw user content and the plugin emits the stamps as a separate
    `lore-time` context message, never inside the user's text."""
    payload = _build_turn_payload(
        model="m", system_prompt="s", tools=[],
        agent_key="lore_x", apply_mode="confirm",
        time_stamps=[
            "[chat started 2026-08-31T18:05:00+03:00]",
            "[sent 2026-09-01T23:14:05+03:00]",
        ],
    )
    assert payload["time_stamps"] == [
        "[chat started 2026-08-31T18:05:00+03:00]",
        "[sent 2026-09-01T23:14:05+03:00]",
    ]
    # Absent stamps degrade to [] (an unconditional key, never a missing field
    # the plugin has to probe for).
    empty = _build_turn_payload(
        model="m", system_prompt="s", tools=[],
        agent_key="lore_x", apply_mode="confirm",
    )
    assert empty["time_stamps"] == []


# ─── agent_capability (configured-or-not — NO probe, plan collapse step 2) ───


@pytest.mark.asyncio
async def test_agent_capability_available_when_configured(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "x")
    from driver.client import agent_capability

    out = await agent_capability()
    assert out["available"] is True


# ─── agent_capability(model) — the DRIVER's capability reply ─────────────────


class _FakeCapabilityResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeCapabilityClient:
    """httpx.AsyncClient stand-in for GET /capability."""

    payload: dict | Exception = {}
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        if isinstance(self.payload, Exception):
            raise self.payload
        return _FakeCapabilityResponse(self.payload)


@pytest.mark.asyncio
async def test_agent_capability_model_bad_reply_raises_unreachable(monkeypatch, http_pool):
    import driver.client

    _FakeCapabilityClient.payload = {"nope": 1}
    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "x")
    monkeypatch.setattr(config, "HARNESS_DRIVER_URL", "http://drv")
    http_pool("driver", _FakeCapabilityClient())

    from driver.client import DriverLineUnreachable

    with pytest.raises(DriverLineUnreachable):
        await driver.client.agent_capability("m1")


# ─── The line is wiring, not configuration (plan component-wiring-not-settings) ──


@pytest.mark.asyncio
async def test_instance_row_cannot_shadow_the_generated_secret(test_db, monkeypatch):
    """Gray defect, observed on a v0.20.3 upgrade: a secret typed in the
    admin panel kept beating the generated file after the upgrade — every
    probe 401, the person saw "harness service is not reachable". The line
    reads the config constant (the secrets volume); an instance_settings row
    is unread."""
    import settings
    from driver.client import resolve_driver_line

    await test_db.query(
        "UPSERT type::record('instance_settings', $k) "
        "SET key = $k, value = $v, updated_by = 'test', updated_at = time::now()",
        {"k": "HARNESS_DRIVER_SECRET", "v": '"typed-in-admin"'},
    )
    settings.drop_cache()
    try:
        # The generated file's value, as a fresh process binds it at import.
        monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "gen-file-value")
        monkeypatch.setattr(config, "HARNESS_DRIVER_URL", "http://harness:8090")
        line = await resolve_driver_line()
        assert line is not None
        assert line.secret == "gen-file-value", (
            "an instance_settings row must not shadow the generated secret"
        )
    finally:
        settings.drop_cache()
        await test_db.query(
            "DELETE type::record('instance_settings', $k)",
            {"k": "HARNESS_DRIVER_SECRET"},
        )


@pytest.mark.asyncio
async def test_capability_401_names_the_secret_mismatch(monkeypatch, http_pool):
    """A 401 from the driver is not generic unreachability: the backend and
    the harness hold different driver secrets, and the exception names the
    recreate-them-together cause (the partner-upgrade defect)."""
    import driver.client
    from driver.client import DriverSecretMismatch

    class _Resp:
        status_code = 401
        text = "unauthorized"

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(config, "HARNESS_DRIVER_SECRET", "x")
    monkeypatch.setattr(config, "HARNESS_DRIVER_URL", "http://drv")
    pin_chat_api(monkeypatch, url="http://gw.example/v1", key="sk-cap")
    http_pool("driver", _Client())

    with pytest.raises(DriverSecretMismatch):
        await driver.client.agent_capability("m1")


# ─── An abnormal end persists the turn's product (incident 78d9a14f) ─────────


@pytest.mark.asyncio
async def test_error_frame_persists_accumulated_text_and_steps(
    client, test_db, project_with_doc,
):
    """The fourth discard path: an explicit driver `error` frame arriving AFTER
    streamed product used to clear finalize_pending with everything accumulated
    so far unwritten. The error frame still relays — and, with a window tail
    behind it, the relay mints the live `lore/halt` card at the tail — while
    the row keeps the product."""
    pid, _, _ = project_with_doc
    msg_id = "pi-msg-error-product"
    await _mk_msg(test_db, msg_id)

    _, frames = await _drive_turn([
        _chunk(1, "partial answer"),
        *_applied_edit_result("c1", doc="d1", seq=2),
        {"type": "error", "message": "gateway 502"},
    ], assistant_msg_id=msg_id)
    # The error frame relays, then the relay mints the halt at the window tail
    # (seq 3, the last dsh event, + the lore/halt offset 0.7).
    assert [f["type"] for f in frames[-2:]] == ["error", "lore/halt"]
    assert frames[-2]["message"] == "gateway 502"
    assert frames[-1]["seq"] == 3.7

    row = await test_db.query(
        "SELECT content, halt FROM type::record('messages', $id)",
        {"id": msg_id},
    )
    assert row[0].get("content") == "partial answer"
    assert (row[0].get("halt") or {}).get("reason") == "error"


# ─── No empty terminal row (incident 78d9a14f, the user-visible half) ─────────
#
# The deadline-breach and line-unreachable variants died with their pump-only
# triggers; their persist semantics (the note-on-empty-product) are the SAME
# abnormal_finalize the error path exercises, and the channel's breach path is
# pinned in test_driver_channel.py.


@pytest.mark.asyncio
async def test_error_frame_with_no_text_stores_terminal_note(
    client, test_db, project_with_doc,
):
    """An error frame before any streamed text (bad model config, provider
    refusal): the driver's own message becomes the stored note, so the row
    records what failed instead of nothing. No dsh event was ever relayed, so
    the error mints NO lore/halt — there is no honest anchor to mint at."""
    pid, _, _ = project_with_doc
    msg_id = "pi-msg-error-note"
    await _mk_msg(test_db, msg_id)

    _, frames = await _drive_turn(
        [{"type": "error", "message": "no configured model gemini/pro"}],
        assistant_msg_id=msg_id,
    )
    assert [f["type"] for f in frames] == ["error"]

    row = await test_db.query(
        "SELECT content, halt FROM type::record('messages', $id)",
        {"id": msg_id},
    )
    assert row[0].get("content") == "no configured model gemini/pro"
    assert (row[0].get("halt") or {}).get("reason") == "error"


# ─── Unknown typed frames relay verbatim (plan collapse-agent-stack step 1) ───
#
# The dsh relay drops NOTHING: a frame type the backend has no branch for
# (tool_call_begin / tool_call_delta from a future driver, or any new kind)
# passes to the browser byte-identical and mutates ZERO projection state
# (content / step count). The dsh_event envelope is the plugin's shape for the
# dsh kinds; THIS branch covers typed frames arriving without a backend branch.


async def _mk_msg(test_db, msg_id):
    await test_db.query(
        "CREATE type::record('messages', $id) SET chat_id='s', role='assistant', "
        "content=''",
        {"id": msg_id},
    )


# harness_env (the driver-channel/post-followup fakes) registers via conftest.
