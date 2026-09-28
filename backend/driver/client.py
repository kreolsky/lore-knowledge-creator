"""Agent-line driver client — the line descriptor + capability answer + turn
payload builder.

# SYSTEM: driver-client — the backend↔driver relay: the backend owns
# identity/RBAC/persistence and relays the driver's frames to the browser
# over the project WS (chat-fanout) and the reload path; the driver owns the
# turn lifecycle.
#
# ARCH: the relay NO LONGER
# TRANSLATES. The plugin emits every dsh event verbatim (`dsh_event` frames)
# plus the lore mints anchored in dsh events, and the BROWSER assembles the
# conversation from them. This package RELAYS verbatim and derives nothing
# from a frame it can pass through. The backend's own share is the
# ACCUMULATION (content, model, context_usage, the finished-call count, the
# driver_seq stamp — frames.py), the two backend-product lore mints
# (`lore/compaction-mint`, and `lore/halt` for a turn that died without a
# dsh terminal event), and the session-title write (the harness titler's
# `session/title` lands in chat_sessions — plan session-title-from-the-harness).
#
# ARCH: the DRIVER owns the turn lifecycle. A turn is POST /followup
# (timeline.py) + the STANDING event channel (channel.py — ONE
# backend→driver /ws/events subscription per lore session, reconnect +
# resync); there is no per-turn HTTP stream.
#
# ARCH: tool calls go DIRECT from the driver to the Tool-API (the backend
# passes the driver the Tool-API base URL + an agent key; it does NOT relay
# each call). Confirmation-mode mutating calls are HELD mid-turn until the
# user's verdict arrives (SYSTEM: apply-policy); an applied result is already
# persisted through the Tool-API's CRDT path.
#
# WHY: every agent turn is routed here and there is no fallback completion
# path. Why: no silent degradation — when the session's line is unconfigured
# it resolves to None and the turn path surfaces an explicit error rather
# than answering from a weaker path; driver/Tool-API/stream errors surface as
# explicit error frames.
#
# This module is the package's thin head: the line descriptor, the
# capability answer and the turn payload builder. The frame arms
# (projection + per-kind relay, FRAME DICTS — the WS vocabulary) live in
# frames.py, the timeline fetchers + turn RPCs (/session-entries, /followup,
# /stop) in timeline.py, and the STANDING event channel (the backend→driver
# /ws/events subscription + resync) in channel.py — every consumer imports
# the owning module.
#
# This module keeps the single `# SYSTEM: driver-client` marker; the sibling
# modules carry plain docstrings (systems-index.py --check enforces ONE
# entry per stack side).
"""
import logging
from dataclasses import dataclass

import http_clients
import httpx
import settings

from models.tools import RegionRef

logger = logging.getLogger(__name__)


# ─── The ONE line ────────────────────────────────────────────────────────────
# driver_lines and driver_health are deleted: with a single line there is
# nothing to choose between, so the address reads straight from config HERE —
# the module that makes every driver RPC. No liveness probe: availability is
# CONFIGURED-or-not, and a down driver surfaces at turn time as the explicit
# line-unavailable refusal (no silent degradation either way).


#: The only line there is — the name errors name and logs carry.
DRIVER_LINE_NAME = "harness"


@dataclass(frozen=True)
class DriverLine:
    """The driver service's transport address + trust secret. `url` is
    normalized (no trailing slash); every RPC appends its own path."""

    name: str
    url: str
    secret: str


class DriverLineUnreachable(RuntimeError):
    """The driver service could not be reached with an answer (DNS failure,
    connection refused, an error status, a malformed reply) — distinct from
    "not configured" (secret unset ⇒ the line resolves to None). Carries the
    line's name so the refusal can be worded for the user: a turn on a
    configured-but-down service must name the line, not the errno."""

    def __init__(self, line_name: str, detail: str) -> None:
        self.line_name = line_name
        super().__init__(f"{line_name} driver unreachable: {detail}")


async def resolve_driver_line() -> DriverLine | None:
    """The configured driver line, or None when unconfigured.

    # INVARIANT(security): an unconfigured line resolves to None — and None means
    # UNCONFIGURED. Why: the routing gate then surfaces an explicit error and
    # the capability signal answers unavailable WITHOUT a probe or any other
    # reachability dependency — a misconfigured deployment must never call an
    # unauthenticated RPC surface, and the address/secret are read through
    # instance settings at CALL time (DB override → env), so a change applies
    # to the next turn/reconnect without a reload.
    """
    secret = ((await settings.get("HARNESS_DRIVER_SECRET")) or "").strip()
    if not secret:
        return None
    return DriverLine(
        name=DRIVER_LINE_NAME,
        url=((await settings.get("HARNESS_DRIVER_URL")) or "").rstrip("/"),
        secret=secret,
    )


async def agent_capability(model: str | None = None) -> dict:
    """Report the agent line's capability: CONFIGURED-or-not, plus — when a
    `model` is asked — the DRIVER's per-model capability reply.

    # ARCH: NO PROBE for the availability answer. With one line
    # there is no fleet to survey, and a down driver is surfaced by the turn
    # itself as the explicit line-unavailable refusal. Availability gating
    # stays truthful: no secret ⇒ unavailable, immediately, with zero
    # reachability dependency.

    # ARCH: with a `model`,
    # the answer is the driver's reply (GET /capability, resolved by the
    # plugin off the gateway /v1/models — the ONE capability source; the
    # Python resolvers that re-read the gateway for the gates are deleted).
    # The gates that read this become read-dependents of the driver: an
    # outage raises DriverLineUnreachable (explicit, never a silently-unarmed
    # gate), while a GATEWAY the driver cannot read degrades driver-side to
    # vision=false — strip + warn, the preserved behavior.
    """
    line = await resolve_driver_line()
    if line is None:
        return {"available": False, "reason": "agent line is not configured"}
    if model is None:
        return {"available": True}
    return {"available": True, **await _driver_capability(line, model)}


#: The capability read budget — short: a gate check, never a turn-length call.
#: The shared "driver" pool client (SYSTEM: http-clients) is built with this;
#: every driver request still passes its own timeout= (the pool invariant).
_CAPABILITY_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def _driver_capability(line: DriverLine, model: str) -> dict:
    """GET /capability on the driver line — the vision gate's ONE read.

    Raises DriverLineUnreachable on ANY failure to answer (connect error,
    error status, malformed reply): a gate that cannot ask the driver must
    surface an explicit error, never guess and never run unarmed."""
    try:
        client = http_clients.get_http_client("driver", timeout=_CAPABILITY_TIMEOUT)
        resp = await client.get(
            f"{line.url}/capability",
            params={"model": model},
            headers={"X-Driver-Secret": line.secret},
            timeout=_CAPABILITY_TIMEOUT,
        )
        resp.raise_for_status()
        reply = resp.json()
        if not isinstance(reply, dict) or not isinstance(reply.get("vision"), bool):
            raise ValueError(f"malformed capability reply: {reply!r}")
        return {"vision": reply["vision"]}
    except Exception as exc:
        raise DriverLineUnreachable(line.name, str(exc)) from exc


# ─── Driver wire (harness POST /followup → frames on /ws/events) ──────────────
# The wire format between the driver service and this client: the plugin
# no longer translates. Every dsh session event arrives VERBATIM as
#   {"type":"dsh_event","kind":<str>,"seq":<n>,"time":<ms>?,"data":<obj>,
#    "surfaceOp"?:…,"sourceEventSeqs"?:[…]}
# — the whole event, no truncation, no hide list — and the BROWSER assembles
# the conversation from these frames (dsh's own ConversationNodeAssembler,
# behind frontend/src/dsh/lore-conversation.js). The driver's own frames:
#   {"type":"model_update","model":<str>}                               # turn model
#   {"type":"context_usage","used":<n>,"cap":<n>}                       # per-turn
#       occupation, emitted right before the terminal frame. Non-terminal.
#   {"type":"lore/verdict-ask","seq":<anchor+0.5>,"data":{turn,callId,toolName},
#       "ignorable":true}                                               # the ONE
#       lore mint anchored inside a dsh session event (approval/asked): the
#       mid-turn ask card is OURS — dsh answers approvals in a composer panel
#       and has no conversation node for the ask.
#   {"type":"lore/halt","seq":<anchor+0.7>,"data":{turn,reason,message?},
#       "ignorable":true}                                               # minted
#       plugin-side on a turn/end whose reason dsh renders no node for
#       (aborted / blocked / interrupted / unknown).
#   {"type":"session_title","title":<str>}                              # the
#       harness titler's revision AFTER the guarded chat-row write landed
#       (the verbatim dsh_event does NOT ride for session/title — the
#       backend's arm replaces it).
#   {"type":"error","message":<str>,"halt_reason":<str>}                # terminal,
#       backend-minted (deadline breach, unreachable line, stream failure,
#       the harness's own catch) — the relay mints the lore halt at the
#       window tail and persists the abnormal product.
# Any OTHER type relays verbatim too (forward-compat): the relay drops nothing
# for being unrendered. The lore mints whose facts are backend products —
# `lore/image-gen` (detached generation), `lore/compaction-mint` (the
# continuation-chat outcome), and the turn-less `lore/halt` — are minted on
# the RELOAD side from the rows (frames.attach_reload_lore_mints).


def _derived_tool_sets() -> dict[str, list[str]]:
    """The turn contract's derived tool sets, serialized unconditionally.

    Each is the single backend source for one driver decision — the driver
    never re-hardcodes any of them (INVARIANT(security) on mutating_tools above;
    region_tools is the sibling for pinned-region confinement)."""
    from agent.tools import HOLDABLE_TOOLS, MUTATING_TOOLS, REGION_TOOLS
    return {
        "mutating_tools": sorted(MUTATING_TOOLS),
        "holdable_tools": sorted(HOLDABLE_TOOLS),
        "region_tools": sorted(REGION_TOOLS),
    }


# The turn-payload contract (why _build_turn_payload's fields are what they are):
# Gateway + Tool-API base URLs are env-bound on the driver side; only the per-user
# agent_key travels in the request. MUTATING_TOOLS is the SINGLE source of truth
# for which tools get `apply` injected — mirrored into the request as
# `mutating_tools` so the driver never re-hardcodes the set.
#
# The turn contract carries NO messages[]: only the last user turn (`prompt`,
# multimodal shape) + the layered system_prompt + the session id — history is
# canonical in the driver's session tree, NOT replayed.
#
# ARCH: assistant_msg_id is the assistant message row this turn writes; the
# driver forwards it as X-Agent-Message-Id on every Tool-API call so a DETACHED
# task (generate_image) can append its chip to that exact message.
#
# NO capability numbers ride the payload: the driver resolves the caps itself
# (plugin caps.ts).
def _build_turn_payload(
    *, model: str, system_prompt: str, tools: list[dict],
    agent_key: str, apply_mode: str, session_id: str = "", user_id: str = "",
    project_id: str = "", document_id: str | None = None,
    prompt: str | list = "", assistant_msg_id: str = "",
    skills: dict | list | None = None, region: RegionRef | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    payload = {
        "model": model,
        "system_prompt": system_prompt,
        "agent_key": agent_key,
        "apply_mode": apply_mode,
        **_derived_tool_sets(),
        "tools": tools,
        "harness": True,
        "session_id": session_id,
        "user_id": user_id,
        "project_id": project_id,
        "document_id": document_id,
        "prompt": prompt,
        "assistant_msg_id": assistant_msg_id,
        # The RAW skills wire: {project, shipped, tombstones} documents — the
        # plugin parses them.
        "skills": skills if skills else [],
        # Pinned-region containment: forwarded on edit/append tool calls.
        # WHY: model_dump — the payload is json.dumps'd, and passing the RegionRef
        # model killed every turn on a pinned session ("not JSON serializable").
        "region": region.model_dump() if region is not None else None,
        # ARCH: present ONLY when the session
        # pins an effort — ABSENT = Default (no reasoning_effort on the wire,
        # the provider's default applies), never a null/'' placeholder (the
        # plugin branches on presence; an empty value would be branded into a
        # ReasoningEffortId).
        **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
    }
    logger.info(
        "_build_turn_payload: session=%s prompt_shape=%s has_images=%s",
        session_id or "new", type(prompt).__name__,
        len(prompt) if isinstance(prompt, list) else "text",
    )
    return payload


# Apply-mode resolution lives in agent.apply_policy.
__all__ = [
    "agent_capability", "_build_turn_payload",
]
