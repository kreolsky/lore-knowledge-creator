"""Driver frame relay — _TurnProjection + the per-kind frame arms (frame DICTS — the WS vocabulary).

Subsystem overview and ARCH notes live in client.py.
See SYSTEM: driver-client (entry: driver/client.py).

# ARCH: the relay is not a
# VOCABULARY. The plugin emits every dsh event verbatim (`dsh_event` frames)
# plus the lore mints anchored in dsh events, and the BROWSER assembles the
# conversation from them. What remains here is the backend's own share:
# ACCUMULATION — the facts the dsh log cannot answer (content, model,
# context_usage, the finished-call count, the driver_seq stamp) are read off
# the verbatim frames as they pass, keyed on what they persist; and the
# lore mints whose facts are backend products (`lore/compaction-mint` — the
# continuation-chat outcome — and `lore/halt` for a turn that ended WITHOUT a
# dsh terminal event: deadline breach, unreachable line, stream failure).
# An unknown frame relays byte-identical; a KNOWN dsh kind whose shape the
# accumulation arm cannot read DEGRADES and is logged — it never raises (a
# turn/start without an int turn logs a warning and keeps the previous
# coordinate, an unreadable chunk accumulates nothing). Why: one mis-shaped
# event must not kill a live turn, so the accumulated row may be incomplete
# while the verbatim relay still reaches the browser.
"""
import logging
import re
import time

# The persistence writes are read through the OWNING module's attribute at
# call time (never a top-level `from driver.persistence import …` — a frozen
# binding makes a patch on `driver.persistence._persist_session_title`
# succeed while inert), so tests string-patch the owner and both consumers
# (this module and the channel) see the fake. Same for the compaction mint —
# the reload/mint tests patch `driver.compaction.mint_compaction_chats`.
from db import extract_id, get_db, record_refs
from driver import compaction, persistence
from driver.timeline import DriverTimelineUnavailable, fetch_session_entries

logger = logging.getLogger(__name__)


class _TurnProjection:
    """The turn's Lore-domain accumulator (content, sources, model, step count).

    # ARCH: this is NOT a transcript. The dsh session log is the canonical
    # timeline and the driver projects it on reload (`POST /session-entries`);
    # the reload==live parity obligation lives in the browser's assembler.
    #
    # What stays is what the dsh log cannot answer, because it is ours:
    # `content` (the product row's own field, read by the chat-list previews
    # through SQL truncation — never through the driver), the sources panel,
    # the active model, and the count of finished tool calls that an ABNORMAL
    # end reports on its halt card.
    """

    def __init__(
        self, *, assistant_msg_id: str, sources: list | None = None,
        persist_content, persist_sources, persist_extras=None,
        persist_turn_seq=None,
        session_id: str = "",
        dsh_session_id: str | None = None,
    ) -> None:
        self.assistant_msg_id = assistant_msg_id
        # The Lore chat session this turn belongs to — threaded for the relay
        # arms that write chat_sessions (the harness title relay) and mint
        # backend-side lore events (the compaction continuation).
        self.session_id = session_id
        # The dsh session this turn runs in — stamped beside driver_seq, since
        # a seq names a boundary only inside its own session's log.
        self.dsh_session_id = dsh_session_id
        self.sources_acc: list[dict] = list(sources or [])
        self.content_acc: list[str] = []
        self.model: str | None = None
        # The dsh log seq of this turn's `turn/end` (the terminal frame's own
        # `seq`) — with dsh_session_id, the row's link into the driver's id
        # space; stamped on every persist path when a terminal frame carried one.
        self.driver_seq: int | None = None
        # INVARIANT: the ONLY thing retained about the tool timeline is HOW
        # MANY calls finished. Why: an abnormal end's halt card reports the
        # honest position ("N tool calls"), and that count is the whole of it —
        # keeping the steps themselves would re-grow the second transcript.
        self.steps_count = 0
        self.finished = False
        self.context_usage: dict | None = None  # last {used,cap}; persisted at the terminal close
        # Set when the turn ends in an ERROR frame — distinct from a graceful
        # halt. Surfaces to the façade so turn-error telemetry fires once per
        # errored turn, not on every finished turn.
        self.errored = False
        # False once the turn ends in a way that must NOT finalize (error /
        # exception). The façade gates finalize() on this flag.
        self.finalize_pending = True
        # The OPEN dsh turn number (turn/start) — the lore mints' turn
        # coordinate.
        self.turn_no: int | None = None
        # The window tail: the last dsh seq relayed this turn. The anchor a
        # backend-minted lore event uses when no terminal frame exists.
        self.last_seq: int | None = None
        self._persist_content = persist_content
        self._persist_sources = persist_sources
        self._persist_extras = persist_extras
        self._persist_turn_seq = persist_turn_seq

    def content(self) -> str:
        """The row's content: the turn's step texts, one paragraph each.

        # INVARIANT: steps join with a blank line, never back to back.
        # Why: between two steps sat a tool run the copy drops; glued, the
        # next step's first line merges into the previous step's last block
        # (a list item swallows the following paragraph).
        """
        return "\n\n".join(p.lstrip("\n").rstrip() for p in self.content_acc if p.strip())

    async def _stamp_driver_seq(self) -> None:
        """Persist the row's driver-turn seq when the terminal frame carried
        one. Skipped for turns that ended without a `turn/end` (deadline
        breach, disconnect: no seq exists) — the fork seam then walks to the
        nearest stamped ancestor instead of reading a fabricated value."""
        if self.driver_seq is not None and self._persist_turn_seq is not None:
            await self._persist_turn_seq(
                self.assistant_msg_id, self.driver_seq, self.dsh_session_id)

    async def finalize(self) -> None:
        """Persist the final assistant content + the accumulated sources panel.

        Called by the façade only on normal turn end. Nothing else is written:
        the turn's timeline is read back from the driver's own log.
        """
        content = self.content()
        await self._persist_content(self.assistant_msg_id, content)
        if self.sources_acc:
            await self._persist_sources(self.assistant_msg_id, self.sources_acc)
        await self._stamp_driver_seq()

    async def _persist_abnormal(self, reason: str, content: str) -> None:
        """The shared abnormal write: content + the halt card naming what
        stopped the turn and how far it got.

        # INVARIANT: an abnormal end is persisted HERE, not read back from the
        # driver. Why: a client disconnect and OUR deadline breach are
        # Lore-side facts the dsh log never records — and an abnormally ended
        # turn usually has no `turn/end`, so the replay yields no entry for it
        # at all. Without this write a reload shows the text with no reason,
        # which is the silent degradation the halt card exists to prevent.
        """
        # No `type` key: the card IS the row's `halt` column — reason/steps
        # carry the shape the renderer takes.
        halt: dict = {"reason": reason}
        if self.steps_count:
            halt["steps"] = self.steps_count
        # INVARIANT(persisted): the halt column stores WHERE the live mint
        # stood — the window tail it anchored at, and the turn it belonged to
        # — whenever each is known. Why: without the stored anchor a reload
        # card's only position is the WHOLE log's tail, which drags it out of
        # its own turn and forces the newest-halted-row-only restriction (two
        # tail mints collide on seq). The stored sentence is NOT part of this:
        # the message lives in the row's `content`, so a reload card shows
        # reason + steps only.
        if self.last_seq is not None:
            halt["anchor_seq"] = self.last_seq
        if self.turn_no is not None:
            halt["turn"] = self.turn_no
        await self._persist_content(self.assistant_msg_id, content)
        await self._stamp_driver_seq()
        if self._persist_extras is not None:
            await self._persist_extras(self.assistant_msg_id, {
                "model": self.model,
                "halt": halt,
            })

    async def abnormal_finalize(self, reason: str, note: str = "") -> None:
        """Persist the turn's product on an ABNORMAL end (deadline breach, line
        unreachable, mid-stream failure, error frame).

        # ARCH: persisting the product is NOT finalize(): the context stamp
        # stays skipped (an abnormal turn has no honest final context state)
        # — but skipping the stamp never meant the work itself should be lost.

        When NOTHING was produced, `note` becomes the content — an empty
        assistant row is never an honest record (the reader cannot tell an
        empty answer from a lost one). The note is the same sentence the live
        error frame carried, so a reload shows exactly what the live user
        saw, naming the deadline/line/stream as the reason — and nothing else.

        Never raises: it runs on a turn that is already failing, and a persist
        error here must not mask the primary failure (logged — not silent).
        """
        try:
            if not self.content():
                self.content_acc.append(note)
            await self._persist_abnormal(reason, self.content())
        except Exception:
            logger.exception(
                "abnormal persist failed msg=%s reason=%s",
                self.assistant_msg_id, reason,
            )

    async def disconnect_finalize(self) -> None:
        """Persist a partial result + position on a mid-stream client disconnect.

        A disconnect kills the turn before the agent composed a final answer, so
        `content` is often empty and (unlike the budget halts) finalize() never ran.
        Without this, a reload shows a silent empty message: the live halt never
        reached the client (it disconnected) and finalize() was skipped. We persist
        the streamed text + a `disconnected` halt card carrying how far the run got,
        so a reload says WHAT stopped and WHERE."""
        await self._persist_abnormal("disconnected", self.content())


def _lore_event_at(kind: str, seq, data: dict) -> dict:
    """One backend-minted lore event at an EXACT seq (the settled events and
    the phase ladder alike). The offsets live in the caller; the envelope is
    the assembler's input (see _lore_event for the parity contract)."""
    return {
        "type": kind,
        "seq": seq,
        "time": int(time.time() * 1000),
        "data": data,
        "ignorable": True,
    }


def _lore_event(kind: str, anchor: int, data: dict) -> dict:
    """One backend-minted lore event as a turn-stream frame. The seq is the anchor
    dsh seq plus the kind's fractional offset — the SAME values the browser's
    assembler expects (LORE_SEQ_OFFSETS in lore-events.ts); the backend mints
    with matching literals because the plugin package's import graph cannot
    reach the backend. test_driver_frames.py pins the parity."""
    offsets = {
        "lore/verdict-ask": 0.5,
        "lore/image-gen": 0.6,
        "lore/halt": 0.7,
        "lore/compaction-mint": 0.8,
    }
    return _lore_event_at(kind, anchor + offsets[kind], data)


async def _relay_frame(turn: _TurnProjection, ev: dict) -> list[dict]:
    """Advance the projection over ONE driver frame and return its frames
    (dicts — the WS frame vocabulary).

    The default is the RELAY: a frame passes to the browser byte-identical.
    The arms below exist only for the backend's OWN share (the module ARCH
    note): accumulation off the verbatim frames, the two backend-product lore
    mints, and the title write."""
    etype = ev.get("type")
    if etype == "dsh_event":
        return await _dsh_event_arm(turn, ev)
    if etype in ("model_update", "context_usage"):
        return _accumulate_frame(turn, ev)
    if etype == "error":
        return await _driver_error_frame(turn, ev)
    # THE RELAY: any other type passes to the browser byte-identical. Nothing
    # is dropped for being unrendered — the lore frames from the plugin ride
    # here, and so would a typed frame from a future driver.
    return [ev]


def _message_text(data: dict) -> str:
    """The text a settled `assistant/message` dsh event carries, or '' — the
    shape the content accumulator keys on: the v4 log holds only settled
    events (deltas never enter the log; the live tail rides `dsh_stream`
    frames the relay passes untouched). Only `text` blocks join: reasoning
    and tool-call
    blocks are not the row's product. The row's `content` column is OURS; the
    dsh log holds the trace itself."""
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _result_call_id(data: dict) -> str:
    """The call id a settled `tool/result` names — its typed `source.callId`."""
    # WHY: dsh session format v4 only — the pin never rolls back, so no reader for an older shape exists; a shape change is fixed forward here.
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    source = message.get("source")
    if isinstance(source, dict) and isinstance(source.get("callId"), str):
        return source["callId"]
    return ""


async def _dsh_event_arm(turn: _TurnProjection, ev: dict) -> list[dict]:
    """Accumulate the backend's own facts off ONE verbatim dsh frame, mint the
    backend-product lore events, and relay the frame (unless an arm replaces
    it — the title write). The frame relays EVEN WHEN an arm minted: the
    browser assembles from the raw events; the mints ride beside them."""
    kind = ev.get("kind")
    data = ev.get("data")
    if not isinstance(data, dict):
        data = {}
    seq = ev.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        turn.last_seq = max(seq, turn.last_seq if turn.last_seq is not None else seq)

    if kind == "turn/start":
        turn_no = data.get("turn")
        if not isinstance(turn_no, int) or isinstance(turn_no, bool):
            # The turn coordinate feeds every lore payload; a missing one
            # mints turn-less facts. Say so instead of storing None silently.
            logger.warning("dsh turn/start without a turn number seq=%s", seq)
        else:
            turn.turn_no = turn_no
        return [ev]
    if kind == "assistant/message":
        # The v4 log holds only settled events: one settled message per step
        # (the whole text, not deltas); the
        # join across the turn's steps is the row's content. Only the
        # `append` surface op joins — the same filter dsh's own assistant
        # node applies (`surfaceOp === 'append'`): a replace-op re-statement
        # of an earlier message would otherwise land in the row twice.
        text = _message_text(data) if ev.get("surfaceOp") == "append" else ""
        if text:
            turn.content_acc.append(text)
        return [ev]
    if kind == "tool/result":
        # The settled call: the ONLY thing the backend keeps of the tool
        # timeline is HOW MANY finished (the abnormal halt card's honest
        # position). The frame itself relays verbatim.
        turn.steps_count += 1
        return [ev]
    if kind == "turn/end":
        return await _turn_end_arm(turn, ev, data)
    if kind == "compaction/end":
        return await _compaction_mint_arm(turn, ev, data)
    if kind == "session/title":
        return await _session_title_arm(turn.session_id, data)
    return [ev]


async def _turn_end_arm(turn: _TurnProjection, ev: dict, data: dict) -> list[dict]:
    """The turn's terminal event: stamp the row's driver_seq and set the
    finalize flags by the reason's PERSISTENCE meaning. The browser derives
    the visible outcome from the same frame (dsh's own turn-error /
    turn-max-tokens nodes, the lore halt the plugin mints for reasons dsh
    renders nothing for) — this arm persists, it does not narrate."""
    seq = ev.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        turn.driver_seq = seq
    reason = data.get("reason")
    rkind = reason.get("kind") if isinstance(reason, dict) else None
    turn.finished = True
    if rkind == "completed" or rkind == "max-tokens":
        # Graceful ends: the terminal close finalizes (content + sources +
        # the context stamp). A max-tokens halt IS graceful — the halt card
        # renders from dsh's own turn-max-tokens node.
        # The `done` frame is the row-content producer for the live client:
        # the SAME join finalize() persists, so the chat-list preview, a copy
        # action and the frameless fallback read the text WITHOUT a reload.
        # Emitted unconditionally on this branch (a tool-only turn carries
        # ""): the store's done handler ignores an empty payload, and
        # suppressing it here would be a second rule to keep in sync with
        # the persist.
        return [ev, {"type": "done", "content": turn.content()}]
    if rkind == "error":
        # No silent degradation — surface the failure explicitly. The product
        # accumulated before the error frame is persisted (abnormal_finalize):
        # an errored turn still owns the text/steps it produced, and with no
        # product the driver's own message becomes the stored note.
        turn.finalize_pending = False
        turn.errored = True
        err = reason.get("error") if isinstance(reason, dict) else None
        message = (
            err.get("message") or err.get("code") or "The agent turn failed."
            if isinstance(err, dict) else "The agent turn failed."
        )
        await turn.abnormal_finalize("error", message)
        return [ev]
    # aborted / blocked / interrupted / unknown: a turn that stopped without
    # its product finalizing. The plugin mints the visible halt card (the same
    # mapEvent the replay runs); here only the honest persistence happens.
    turn.finalize_pending = False
    turn.errored = True
    message = (
        reason.get("error", {}).get("message")
        if isinstance(reason, dict) and isinstance(reason.get("error"), dict)
        else None
    ) or f"The turn ended without completing ({rkind or 'unknown reason'})."
    await turn.abnormal_finalize(str(rkind or "unknown"), message)
    return [ev]


async def _compaction_mint_outcome(
    session_id: str, compaction_id: str,
) -> tuple[bool, str | None]:
    """Run the window-keyed continuation mint and read its outcome — the
    shared core of BOTH mint sites (the live relay arm and the reload attach:
    the same window key is what makes a replayed/reloaded compaction mint
    nothing new). Returns (mint_failed, mint_reason). Never raises: the mint
    must not break a turn (or a read)."""
    try:
        # The mint contract requires the identity pair: the lore session plus
        # a driver-side fork id. dsh compacts IN-PLACE (no file fork), so the
        # "fork" is the compaction's own id — the SAME value as the window key
        # (which is what makes a replayed frame mint nothing new).
        minted = await compaction.mint_compaction_chats({
            "session_id": session_id,
            "fork_id": f"{session_id}~c{compaction_id}",
        })
        if not minted.get("continuation_chat_id"):
            return True, str(minted.get("reason") or "unknown")
        return False, None
    except Exception as exc:
        logger.warning(
            "compaction chat mint failed session=%s", session_id, exc_info=True,
        )
        return True, repr(exc)


async def _compaction_mint_arm(turn: _TurnProjection, ev: dict, data: dict) -> list[dict]:
    """The compaction lifecycle's own closer IS the mint trigger: dsh's
    compactionId is the window key — stable per compaction, so a replayed
    frame keys the same window and the backend mints nothing new. A FAILED
    compaction (error) mints nothing and relays neutrally.

    The mint OUTCOME is a Lore product the dsh log cannot state, so it is
    minted as `lore/compaction-mint` at the compaction/end seq (+0.8) and
    rides to the browser beside the verbatim frame — the SAME anchor the
    reload attach mints (anchor-parity). Best-effort: the mint must never
    break the turn — the frame relays regardless.
    # WHY: a mint failure is REPORTED, never swallowed. The mint must not
    # break the turn (compaction already mutated the transcript — failing
    # here would lose the turn on top of it), but a silent failure leaves
    # the user a "context summarized" notice with no archived chat behind
    # it. The lore payload carries mint_failed so the frontend warns, and
    # the telemetry row is what makes a chronic failure visible."""
    compaction_id = data.get("compactionId")
    if not isinstance(compaction_id, str) or not compaction_id or data.get("error"):
        return [ev]
    mint_failed, mint_reason = await _compaction_mint_outcome(
        turn.session_id, compaction_id,
    )
    if mint_failed:
        await persistence._record_compaction_mint_failed(turn.assistant_msg_id, mint_reason or "unknown")
    anchor = ev.get("seq")
    out = [ev]
    if isinstance(anchor, int) and not isinstance(anchor, bool):
        out.append(_lore_event("lore/compaction-mint", anchor, {
            "turn": turn.turn_no,
            "compactionEntryId": compaction_id,
            "mintFailed": mint_failed,
            **({"mintReason": mint_reason} if mint_reason else {}),
        }))
    return out


async def _session_title_arm(session_id: str, data: dict) -> list[dict]:
    """The harness titler's revision: write it onto the chat row, then relay it.

    `chat_sessions.title` is where a RELOAD reads the title, and the open
    client is the other reader — it holds the sessions list in a store the
    write cannot reach, so a turn that renames the chat would leave the list
    showing the fallback until the next full fetch. The relayed frame is what
    closes that: the store applies it to the row live. The VERBATIM dsh_event
    does not ride (a naming decision shows no chip; the assembler builds no
    node for the kind) — the typed frame below is the title's transport only.

    The leak guard lives HERE now (it moved with the translation's death): a
    tokenizer-leak title must never reach the user — the chat list would
    surface `<unused49><unused49>…` where a real title should be. Detected →
    no write, no frame → the chat row keeps the deterministic fallback title.

    INVARIANT: the frame is relayed only when the write LANDED. Why: a row
    pinned by a user rename (title_user_set) keeps its name, and relaying a
    refused revision would put on screen a title the DB does not hold — the
    next reload would silently change it back.

    Best-effort on the write: a persist failure must not break the turn (the
    row keeps the deterministic fallback title the service populated before
    any model call), and nothing is relayed for one.
    """
    title = data.get("title")
    if not isinstance(title, str):
        return []
    trimmed = title.strip()
    if not trimmed or any(p.search(trimmed) for p in _TITLE_LEAK_PATTERNS):
        return []
    try:
        written = await persistence._persist_session_title(session_id, trimmed)
    except Exception:
        logger.warning(
            "session title persist failed session=%s", session_id,
            exc_info=True,
        )
        return []
    if not written:
        logger.info(
            "session title not written (pinned or empty) session=%s",
            session_id,
        )
        return []
    return [{"type": "session_title", "title": trimmed}]


# Tokenizer leakage a model-written title must never carry: Gemma SentencePiece
# unused vocab slots and raw byte tokens (Qwen / Llama-style).
_TITLE_LEAK_PATTERNS = [
    re.compile(r"<unused\d+>"),
    re.compile(r"<0x[0-9a-fA-F]+>"),
]


def _accumulate_frame(turn: _TurnProjection, ev: dict) -> list[dict]:
    """Frames whose only backend work is projection accumulation — relayed
    verbatim while the accumulator advances. The DRIVER's own frames
    (`model_update`, `context_usage`) — not dsh events."""
    etype = ev.get("type")
    if etype == "model_update":
        # The active model for the turn (persisted to `messages.model`).
        turn.model = ev.get("model") or turn.model
        return [ev]
    # context_usage: per-turn occupation signal (before done/error) — capture
    # for persistence, relay verbatim. Non-terminal.
    used = int(ev.get("used") or 0)
    cap = int(ev.get("cap") or 0)
    turn.context_usage = {"used": used, "cap": cap}
    return [ev]


async def _driver_error_frame(turn: _TurnProjection, ev: dict) -> list[dict]:
    """The backend-minted `error` frame — the driver-level failure the dsh log
    never records (deadline breach, unreachable line, stream failure, the
    harness's own catch). Persists the abnormal product and mints the lore
    halt card at the window tail (the halt anchor: "the terminal frame's seq
    or the window tail when none exists" — here none exists). The
    error frame itself still relays: it is the live-only end-state signal
    (toast, endReason), while the lore card is the timeline's record."""
    turn.finished = True
    turn.finalize_pending = False
    turn.errored = True
    message = ev.get("message") or "The agent turn failed."
    await turn.abnormal_finalize(str(ev.get("halt_reason") or "error"), message)
    out = [ev]
    if turn.last_seq is not None:
        out.append(_lore_event("lore/halt", turn.last_seq, {
            "turn": turn.turn_no,
            "reason": str(ev.get("halt_reason") or "error"),
            "message": message,
            **({"steps": turn.steps_count} if turn.steps_count else {}),
        }))
    return out


# ─── The reload side of the lore mints (the timeline attach) ──────────────────
#
# # ARCH: the plugin's replay
# relays the dsh log verbatim, but three lore facts are BACKEND products the
# log cannot state — the same three the live path minted beside the stream:
# `lore/compaction-mint` (the continuation-chat outcome), `lore/halt` for a
# turn that ended without a dsh terminal event (the row's `halt` column is
# that turn's only Lore-side record), and `lore/image-gen` (the detached
# generation the driver's log never saw, persisted in the row's `gen_steps`).
# The timeline attach mints them FROM THE ROWS and rides them on the row's
# `frames`, so the browser's ONE replaceWindow input carries the mints at the
# same anchors the live stream used (anchor-parity: reload==live is the
# producer minting the same anchor on both paths).


def _image_run_anchor_index(frames: list[dict], call_id: str) -> int | None:
    """The index of the run's dispatching `tool/call` frame — the one whose
    `callId` is the call id the Tool-API received (X-Agent-Call-Id) and the
    generate_image step persisted. This is the ONE join both mint sites share:
    the reload attach and the launcher's anchor resolve both walk the same
    replayed frames."""
    for i, frame in enumerate(frames):
        if (isinstance(frame, dict) and frame.get("kind") == "tool/call"
                and (frame.get("data") or {}).get("callId") == call_id):
            return i
    return None


def _image_ref_fields(ids: list[str], live_refs: set[str] | None) -> dict:
    """The done-run's image fields per the live set (see _image_gen_frame):
    no set → the ids verbatim (the worker's brand-new images); a set → the
    live ids in order, or the all-deleted note when none survive."""
    if live_refs is None:
        return {"imageRefIds": ids}
    live = [r for r in ids if r in live_refs]
    return {"imageRefIds": live} if live else {"deletedByUser": True}


def _image_gen_frame(anchor_frame: dict, gen_steps: list, run_id: str,
                     live_refs: set[str] | None = None) -> dict | None:
    """One `lore/image-gen` frame minted at the run's dispatching call.

    The payload derives from the row's `gen_steps` step dicts — the SAME dicts
    the worker's settled frame passes (image_gen_settled_frame) and the row
    persists, so the live card and the reload card agree by construction.
    Returns None when the run has no chip in gen_steps (nothing honest to
    render).

    # ARCH: references are the truth on delete. `gen_steps` is written once
    # and never rewritten, so the RELOAD pass filters the ids through
    # `live_refs` (the page's still-live reference set, one query — see
    # attach_reload_lore_mints): a deleted reference drops out of the mint,
    # and a run whose images were ALL deleted flips to `deletedByUser` (the
    # chip keeps its plate with the note). `live_refs=None` — the worker's
    # settled pass — keeps the ids verbatim: those images are brand new."""
    gen = None
    for step in gen_steps:
        if (isinstance(step, dict) and step.get("tool") == "generate_image"
                and step.get("run_id") == run_id):
            gen = step
    if gen is None:
        return None
    failed = gen.get("outcome") == "failed"
    data: dict = {
        "turn": anchor_frame.get("data", {}).get("turn")
        if isinstance(anchor_frame.get("data"), dict) else None,
        "runId": run_id,
        "status": "failed" if failed else "done",
    }
    if failed:
        if gen.get("detail"):
            data["error"] = str(gen["detail"])
    elif isinstance(gen.get("image_ref_ids"), list) and gen["image_ref_ids"]:
        data.update(_image_ref_fields([str(r) for r in gen["image_ref_ids"]], live_refs))
    for step in gen_steps:
        if isinstance(step, dict) and step.get("tool") == "refine_prompt":
            ok = step.get("outcome") != "failed"
            refine: dict = {"ok": ok}
            detail = step.get("detail")
            if detail:
                refine["prompt" if ok else "error"] = str(detail)
            data["refine"] = refine
    if isinstance(gen.get("title"), str) and gen["title"]:
        data["title"] = gen["title"]
    anchor = anchor_frame.get("seq")
    if not isinstance(anchor, int) or isinstance(anchor, bool):
        return None
    return _lore_event("lore/image-gen", anchor, data)


def _replay_anchor_frame(replay: dict, call_id: str) -> dict | None:
    """The dispatching `tool/call` frame anywhere in the replay — every turn's
    frames, the OPEN trailing turn included (the run settles while the agent
    turn still streams; dsh writes the `tool/call` before executing the
    tool)."""
    for turn in replay.get("turns") or []:
        frames = turn.get("frames") if isinstance(turn, dict) else None
        if not isinstance(frames, list):
            continue
        idx = _image_run_anchor_index(frames, call_id)
        if idx is not None:
            return frames[idx]
    return None


# The run's coarse phases, in execution order — the RUNNING frame ladder's
# step counter (image_gen_running_frame).
_IMAGE_GEN_PHASES = ("refining", "queued", "generating", "downloading")


def image_gen_running_frame(anchor: dict, run_id: str, phase: str) -> dict | None:
    """One RUNNING `lore/image-gen` frame for the run's k-th phase, minted
    from the payload anchor (no replay read per phase). The phase ladder: k sits at anchor + 0.5 + 0.1·k/(k+1) — dsh's
    own transient formula (the live-chunk counter) — STRICTLY above a verdict
    mint at the same anchor, STRICTLY below the settled +0.6 mint (the
    reload's twin), and strictly increasing, so every phase of one run
    APPENDS into the assembler's one context (a duplicate or non-appended
    Match seq throws there). None when the anchor carries no usable seq (the
    launcher's resolve missed — nothing honest to place)."""
    seq = anchor.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    try:
        k = _IMAGE_GEN_PHASES.index(phase) + 1
    except ValueError:
        k = len(_IMAGE_GEN_PHASES) + 1
    return _lore_event_at("lore/image-gen", seq + 0.5 + 0.1 * k / (k + 1), {
        "turn": _int_or_none(anchor.get("turn")),
        "runId": run_id,
        "status": "running",
        "phase": phase,
    })


async def resolve_image_gen_anchor(lineage: str, call_id: str) -> dict | None:
    """The ANCHOR half of the image-gen mint: the dispatching `tool/call`'s {seq, turn} over the driver's replay of the
    chat's LINEAGE log — the one join the reload attach shares. The launcher
    calls this ONCE (where the X-Agent-Call-Id is known and the call is
    already in the log) and freezes the result into the job payload, so the
    worker never reads the timeline. Best-effort like the mint: an
    unreadable timeline or a missing call logs a warning naming the call and
    returns None — no anchor, no card (a reload still shows it from the
    row)."""
    if not call_id:
        return None
    try:
        replay = await fetch_session_entries(lineage)
    except DriverTimelineUnavailable as exc:
        logger.warning(
            "image-gen anchor resolve: timeline unavailable lineage=%s call=%s: %s",
            lineage, call_id, exc,
        )
        return None
    anchor = _replay_anchor_frame(replay, call_id)
    if anchor is None:
        logger.warning(
            "image-gen anchor resolve: no dispatching call in the replay "
            "lineage=%s call=%s", lineage, call_id,
        )
        return None
    seq = anchor.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    return {"seq": seq, "turn": _int_or_none(
        anchor.get("data", {}).get("turn") if isinstance(anchor.get("data"), dict) else None,
    )}


def image_gen_settled_frame(anchor: dict, gen_steps: list, run_id: str) -> dict | None:
    """The run's SETTLED `lore/image-gen` frame (done or failed — the step
    dicts decide), minted from the payload anchor through the SAME builder
    the reload attach uses (_image_gen_frame over the same gen_steps), so the
    pushed card is byte-identical to the one a reload re-derives: one
    producer cannot disagree with itself. None when the anchor carries no
    usable seq — the run's chips are still persisted; only the live card is
    skipped."""
    seq = anchor.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    return _image_gen_frame({"seq": seq, "data": {"turn": anchor.get("turn")}},
                            gen_steps, run_id)


async def _reload_compaction_frame(
    session_id: str, frame: dict,
) -> dict | None:
    """Re-mint ONE compaction/end's outcome on reload. The frame relays
    verbatim from the replay; the OUTCOME is a backend product — re-running
    the window-keyed mint is the honest current state (an already-minted
    window returns the existing row; a live-mint failure that healed since
    mints now)."""
    data = frame.get("data") or {}
    compaction_id = data.get("compactionId")
    if not isinstance(compaction_id, str) or not compaction_id or data.get("error"):
        return None
    seq = frame.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    mint_failed, mint_reason = await _compaction_mint_outcome(session_id, compaction_id)
    turn_no = data.get("turn")
    return _lore_event("lore/compaction-mint", seq, {
        "turn": turn_no if isinstance(turn_no, int) and not isinstance(turn_no, bool) else None,
        "compactionEntryId": compaction_id,
        "mintFailed": mint_failed,
        **({"mintReason": mint_reason} if mint_reason else {}),
    })


def _row_identity(row: dict) -> str:
    """The message id of a raw or serialized row (the slim projection carries
    `mid`, the serialized page carries `message_id`)."""
    mid = row.get("message_id") or row.get("mid")
    return str(mid or "")


def _halt_mint_data(halt: dict, turn_no: int | None) -> dict:
    """The reload halt card's payload: reason + steps, at the turn coordinate
    the row stored (None for a row halted before it was stored)."""
    data: dict = {"turn": turn_no, "reason": str(halt.get("reason") or "unknown")}
    steps = halt.get("steps")
    if isinstance(steps, int) and not isinstance(steps, bool) and steps:
        data["steps"] = steps
    return data


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _attach_halt_mints(
    assistants: list[dict], *, tail_seq: int | None,
) -> None:
    """Mint `lore/halt` onto the qualifying rows — see the placement INVARIANT
    in attach_reload_lore_mints."""
    # The qualifier: halt column present, no resolved turn (no frames were
    # assigned — dsh's own turn-error node renders those).
    halt_rows = [
        row for row in assistants
        if isinstance(row.get("halt"), dict)
        and row.get("frames") is None
    ]
    halt_rows.sort(key=lambda r: r.get("created_at") or "")
    taken: dict[int, str] = {}
    legacy: list[dict] = []
    for row in halt_rows:
        anchor = _int_or_none(row["halt"].get("anchor_seq"))
        if anchor is None:
            legacy.append(row)
            continue
        # INVARIANT(corruption): no two `lore/halt` mints may share a seq.
        # Why: the assembler throws on a duplicate Match, which would break
        # the whole window (the lore-events identity INVARIANT) — a dropped
        # card costs one row's record, a duplicate costs the timeline.
        if anchor in taken:
            logger.warning(
                "halt row %s anchors at seq %s, already taken by %s — no reload "
                "card for it (two lore/halt mints at one seq break the window)",
                _row_identity(row), anchor, taken[anchor],
            )
            continue
        taken[anchor] = _row_identity(row)
        row["frames"] = [_lore_event(
            "lore/halt", anchor, _halt_mint_data(row["halt"], _int_or_none(row["halt"].get("turn"))))]
    tail = _int_or_none(tail_seq)
    if not legacy or tail is None:
        return
    newest = legacy[-1]
    if tail in taken:
        logger.warning(
            "halt row %s has no stored anchor and the log tail %s is taken by "
            "%s — no reload card for it (its halt column remains its record)",
            _row_identity(newest), tail, taken[tail],
        )
        return
    newest["frames"] = [_lore_event("lore/halt", tail, _halt_mint_data(newest["halt"], None))]


async def _live_image_ref_ids(ids: list[str]) -> set[str]:
    """The LIVE subset of a page's image reference ids — ONE projected query
    (`id` only, soft-deleted rows excluded). Raises on DB failure; the caller
    (attach_reload_lore_mints) degrades to the ids verbatim."""
    refs, params = record_refs("documents", ids)
    db = await get_db()
    rows = await db.query(
        f"SELECT id FROM documents WHERE id IN [{refs}] AND deleted_at IS NONE",
        params,
    )
    live = {extract_id(r.get("id")) for r in rows or []}
    live.discard(None)
    return live


def _insert_image_gen_mints(out: list[dict], gen_steps, live_refs=None) -> None:
    """Insert one `lore/image-gen` card per detached run this turn dispatched,
    each after its dispatching `tool/call` frame. Mutates `out`. `live_refs`
    (the reload pass) filters deleted references — see _image_gen_frame."""
    if not isinstance(gen_steps, list) or not gen_steps:
        return
    runs: dict[str, str] = {}
    for step in gen_steps:
        if not isinstance(step, dict) or step.get("tool") != "generate_image":
            continue
        rid = step.get("run_id")
        if isinstance(rid, str) and rid and rid not in runs:
            cid = step.get("call_id")
            runs[rid] = cid if isinstance(cid, str) else ""
    for rid, cid in runs.items():
        idx = _image_run_anchor_index(out, cid) if cid else None
        if idx is None:
            logger.warning(
                "image run %s has no dispatching call in the replayed "
                "turn (call id %r) — no reload card (the reference itself "
                "is on the document)", rid, cid,
            )
            continue
        mint = _image_gen_frame(out[idx], gen_steps, rid, live_refs)
        if mint is not None:
            out.insert(idx + 1, mint)


def _page_image_ref_ids(assistants: list[dict]) -> list[str]:
    """Every image reference id the page's gen_steps carry, de-duplicated in
    row order — the ONE id list the reload's live-ref query runs over."""
    ids: list[str] = []
    seen: set[str] = set()
    for row in assistants:
        steps = row.get("gen_steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if (isinstance(step, dict) and step.get("tool") == "generate_image"
                    and isinstance(step.get("image_ref_ids"), list)):
                for ref in step["image_ref_ids"]:
                    rid = str(ref)
                    if rid not in seen:
                        seen.add(rid)
                        ids.append(rid)
    return ids


async def attach_reload_lore_mints(
    assistants: list[dict], *,
    tail_seq: int | None, session_id: str,
) -> None:
    """Mint the backend-product lore events onto the attached rows, in place.

    `assistants` are the SERIALIZED assistant rows of the page (carrying
    `halt`, `gen_steps` and — for turns the replay resolved — `frames`).
    Mutates the rows' `frames`.

    The halt placement: a row whose halt stored the live mint's anchor mints
    THERE, inside its own turn — so every such halted turn keeps its card. A
    row halted before the anchor was stored has no position but the log tail
    (the "window tail when none exists" rule), so at most ONE of THEM
    mints, the newest: once a later turn re-seeds the log, an older turn-less
    halt's position does not exist anywhere — the row's `halt` column
    remains its record. A card carries reason + steps, never the live
    sentence (that is the row's `content`). See the seq-collision INVARIANT
    in _attach_halt_mints.
    """
    _attach_halt_mints(assistants, tail_seq=tail_seq)

    # The page's deleted-image filter: references are the truth, gen_steps is
    # not rewritten — ONE live-ref query per page (None while the page holds
    # no image run, or when the query fails: the ids then stay verbatim).
    live_refs: set[str] | None = None
    page_ref_ids = _page_image_ref_ids(assistants)
    if page_ref_ids:
        try:
            live_refs = await _live_image_ref_ids(page_ref_ids)
        except Exception:
            logger.warning(
                "image-gen reload mint: live-ref query failed for %d ids — "
                "gen_steps ids kept verbatim", len(page_ref_ids), exc_info=True,
            )
            live_refs = None

    for row in assistants:
        frames = row.get("frames")
        if not isinstance(frames, list) or not frames:
            continue
        out = list(frames)
        _insert_image_gen_mints(out, row.get("gen_steps"), live_refs)
        # The compaction windows this turn closed: one outcome card each.
        for frame in [f for f in out if isinstance(f, dict) and f.get("kind") == "compaction/end"]:
            mint = await _reload_compaction_frame(session_id, frame)
            if mint is not None:
                out.insert(out.index(frame) + 1, mint)
        row["frames"] = out

