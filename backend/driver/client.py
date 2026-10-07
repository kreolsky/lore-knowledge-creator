"""Agent-line driver client — the line descriptor + capability answer + turn
payload builder.

# SYSTEM: driver-client — the backend↔driver relay: the backend owns
# identity/RBAC/persistence and relays the driver's frames to the browser
# over the project WS (chat-fanout) and the reload path; the driver owns the
# turn lifecycle.
#
# ARCH: the relay translates
# NOTHING. The plugin emits every dsh event verbatim (`dsh_event` frames)
# plus the lore mints anchored in dsh events, and the BROWSER assembles the
# conversation from them. This package RELAYS verbatim and derives nothing
# from a frame it can pass through. The backend's own share is the
# ACCUMULATION (content, model, context_usage, the finished-call count, the
# driver_seq stamp — frames.py), the backend-product lore mints
# (`lore/compaction-mint`, `lore/halt` for a turn that died without a dsh
# terminal event, and `lore/image-gen` for the detached generation), and the
# session-title write (the harness titler's `session/title` lands in
# chat_sessions).
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

import config
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


class DriverSecretMismatch(DriverLineUnreachable):
    """HTTP 401 from the driver: the backend and the harness hold different
    driver secrets — the pair was recreated one side at a time (or an old
    install's typed secret survived the upgrade). Both read the secrets
    volume, so the fix is recreating them together, and every refusal names
    that cause with `CAUSE` instead of the generic not-reachable wording."""

    CAUSE = (
        "Agent line unavailable — the backend and the harness hold different "
        "driver secrets. Both read the secrets volume: recreate them together "
        "(`docker compose up -d`; an install that builds the harness also "
        "needs `--build`)."
    )

    def __init__(self, line_name: str) -> None:
        super().__init__(line_name, "HTTP 401 — driver secret mismatch")


async def resolve_driver_line() -> DriverLine | None:
    """The configured driver line, or None when unconfigured.

    # INVARIANT(security): an unconfigured line resolves to None — and None means
    # UNCONFIGURED. Why: the routing gate then surfaces an explicit error and
    # the capability signal answers unavailable WITHOUT a probe or any other
    # reachability dependency — a misconfigured deployment must never call an
    # unauthenticated RPC surface. The address and secret are not
    # configuration (see the INVARIANT above the agent section in config.py):
    # constants bound at import off the compose service name and the
    # generated secret file — read through config at call time so the value
    # is the module's, not a stale from-import.
    """
    secret = (config.HARNESS_DRIVER_SECRET or "").strip()
    if not secret:
        return None
    return DriverLine(
        name=DRIVER_LINE_NAME,
        url=config.HARNESS_DRIVER_URL.rstrip("/"),
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
    # The gateway rides as headers (admin panel, else env — the turn
    # payload's source), so the key never lands in a URL or an access log.
    gateway = await settings.get_all(["AI_API_URL", "AI_API_KEY"])
    try:
        client = http_clients.get_http_client("driver", timeout=_CAPABILITY_TIMEOUT)
        resp = await client.get(
            f"{line.url}/capability",
            params={"model": model},
            headers={
                "X-Driver-Secret": line.secret,
                "X-AI-API-URL": gateway["AI_API_URL"],
                "X-AI-API-Key": gateway["AI_API_KEY"],
            },
            timeout=_CAPABILITY_TIMEOUT,
        )
        # 401 is the secret mismatch — named, not folded into "unreachable"
        # (the service is up and refusing; the pair must be recreated).
        if resp.status_code == 401:
            raise DriverSecretMismatch(line.name)
        resp.raise_for_status()
        reply = resp.json()
        if not isinstance(reply, dict) or not isinstance(reply.get("vision"), bool):
            raise ValueError(f"malformed capability reply: {reply!r}")
        return {"vision": reply["vision"]}
    except DriverSecretMismatch:
        raise
    except Exception as exc:
        raise DriverLineUnreachable(line.name, str(exc)) from exc


# ─── Driver wire (harness POST /followup → frames on /ws/events) ──────────────
# The wire format between the driver service and this client: the plugin
# translates nothing. Every dsh session event arrives VERBATIM as
#   {"type":"dsh_event","kind":<str>,"seq":<n>,"time":<ms>?,"data":<obj>,
#    "surfaceOp"?:…,"sourceEventSeqs"?:[…]}
# — the whole event, no truncation, no hide list — and the BROWSER assembles
# the conversation from these frames (dsh's own ConversationNodeAssembler,
# behind frontend/src/dsh/lore-conversation.js). The driver's own frames:
#   {"type":"model_update","model":<str>}                               # turn model
#   {"type":"context_usage","used":<n>,"cap":<n>}                       # per-turn
#       occupation, emitted right before the terminal frame. Non-terminal.
#   {"type":"dsh_stream","frame":<obj>}                                 # the
#       transient live tail: dsh's own agent/assistant-stream publication
#       relayed by the plugin, unsequenced, never replayed. Non-terminal.
#   {"type":"lore/verdict-ask","seq":<anchor+0.5>,"data":{turn,callId,toolName},
#       "ignorable":true}                                               # a lore
#       mint anchored inside a dsh session event (approval/asked): the
#       mid-turn ask card is OURS — dsh answers approvals in a composer panel
#       and has no conversation node for the ask.
#   {"type":"lore/halt","seq":<anchor+0.7>,"data":{turn,reason,message?},
#       "ignorable":true}                                               # three
#       producers: the plugin (a turn/end whose reason dsh renders no node
#       for — aborted / blocked / interrupted / unknown), the backend's live
#       relay (a turn that ended with NO dsh terminal event — frames.py), and
#       the backend's reload attach (the row's `halt` column).
#   {"type":"session_title","title":<str>}                              # the
#       harness titler's revision AFTER the guarded chat-row write landed
#       (the verbatim dsh_event does NOT ride for session/title — the
#       backend's arm replaces it).
#   {"type":"turn_closed"}                                              # TERMINAL
#       (a browser terminal): pushed by the plugin after the turn's last
#       mapped frame — a driver-owned turn has no stream whose end closes
#       it — and re-minted by the backend where the push cannot arrive: the
#       deadline breach (driver.channel) and the resync close re-mint (a
#       turn/end lost mid-gap, or a turn that ended live before the gap —
#       the owed-close flag in driver.channel._resync_all).
#   {"type":"error","message":<str>,"halt_reason":<str>}                # TERMINAL,
#       backend-minted (deadline breach, unreachable line, stream failure,
#       the harness's own catch) — the relay mints the lore halt at the
#       window tail and persists the abnormal product.
# Any OTHER type relays verbatim too (forward-compat): the relay drops nothing
# for being unrendered. The browser's ONLY terminal is `turn_closed`. The
# backend also feeds the SAME chat channel beside the driver's frames: the
# turn preamble (completions_harness.py — `ids` names the user/assistant row
# pair, `sources` the context panel, `context_warning`; non-terminal), `done`
# (frames.py — a CONTENT frame, not a terminal: carries the row's joined
# content on a graceful end, emitted before finalize + the turn-lock release)
# and the setup-failure tail `error` + `turn_closed` (completions_harness.py).
# The lore mints whose facts are backend products — `lore/image-gen` (the
# detached generation: live as the run's frames ride the owner's chat
# channel, and on reload from the row's gen_steps), `lore/compaction-mint`
# (the continuation-chat outcome), and the
# turn-less `lore/halt` — are ALSO minted on the RELOAD side from the rows
# (frames.attach_reload_lore_mints).


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


#: WEB_SEARCH_PROVIDER value → (the dsh provider id the harness pins, the
#: setting that is its credential — the key, or the URL for SearXNG). The
#: turn payload builder is the one reader.
WEB_SEARCH_PROVIDERS: dict[str, tuple[str, str]] = {
    "deepseek": ("deepseek-official", "DEEPSEEK_API_KEY"),
    "brave": ("lore-brave", "BRAVE_API_KEY"),
    "tavily": ("lore-tavily", "TAVILY_API_KEY"),
    "searxng": ("lore-searxng", "SEARXNG_URL"),
}


def _admin_config_fields(
    title_model: str, web_search_provider: str, web_search_credential: str,
) -> dict:
    """The per-turn admin-config trio (always present; see WEB_SEARCH_PROVIDERS
    above): an empty title_model = the session's own model titles, an empty
    credential = the loud no-key failure — no off state."""
    return {
        "title_model": title_model,
        "web_search_provider": web_search_provider,
        "web_search_credential": web_search_credential,
    }


# The turn-payload contract (why _build_turn_payload's fields are what they are):
# The Tool-API base URL is env-bound on the driver side; the per-user agent_key
# travels in the request. The AI endpoint + key travel too: the admin panel
# owns them (settings.get — admin override, else env), and the turn
# is the one path by which an admin change reaches the harness without a restart.
# The session title model and the web-search provider + its ONE credential ride
# the SAME path (title_model keeps config.py's CHAT_MODEL fallback; an unset
# credential rides as '' — web search has no off state, the harness fails the
# call loudly naming the provider and the admin path).
# INVARIANT(security): ai_api_key and web_search_credential never reach a log
# line or a frame. Why: they are the operator's credentials; /followup is
# gated by the driver secret, the log is not. MUTATING_TOOLS is the SINGLE
# source of truth for which tools get `apply` injected — mirrored into the
# request as `mutating_tools` so the driver never re-hardcodes the set.
#
# The turn contract carries NO messages[]: only the last user turn (`prompt`,
# multimodal shape — the RAW user text) + the layered system_prompt + the
# session id — history is canonical in the driver's session tree, NOT replayed.
# The turn's time stamps ride `time_stamps` as their own field — see ARCH in
# harness-driver/plugin/src/time-stamps.ts.
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
    time_stamps: list[str] | None = None,
    skills: dict | list | None = None, region: RegionRef | None = None,
    reasoning_effort: str | None = None,
    ai_api_url: str = "", ai_api_key: str = "",
    title_model: str = "", web_search_provider: str = "",
    web_search_credential: str = "",
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
        "time_stamps": time_stamps or [],
        "ai_api_url": ai_api_url,
        "ai_api_key": ai_api_key,
        **_admin_config_fields(title_model, web_search_provider, web_search_credential),
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
