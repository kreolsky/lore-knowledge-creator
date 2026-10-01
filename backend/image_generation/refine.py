"""Image generation prompt refinement (the D6 refiner).

Subsystem overview: image_generation/__init__.py (see SYSTEM: comfy-image-gen).
The chat line (URL/KEY) resolves through the settings CHAT→AI→STT chain and the
refinement model through the COMFYUI_PROMPT_MODEL→CHAT_MODEL chain at call time
(tests pin the config.* bucket — the settings fallback legs); the shared pool's
"comfy_prompt" client is faked through the one http_pool seam (see SYSTEM:
http-clients).
"""

import logging

import http_clients
import settings

from image_generation.image_refine import (
    _REFINE_PROMPT_SCHEMA,
    _parse_prompt_json,
    _RefineResult,
)

logger = logging.getLogger(__name__)

# ─── D6: prompt refinement ────────────────────────────────────────────────────
# The agent's `prompt` is a complete scene description; the refiner expands it
# into an SD prompt using the COMFYUI_PROMPT admin setting as the system message.
# The template is used VERBATIM — there is no
# placeholder substitution and no DB read (see the INVARIANT in the module
# docstring).

async def _refine_prompt(seed: str, template: str) -> _RefineResult:
    """Expand the seed — a complete scene description — into a full SD prompt.

    The `template` (the COMFYUI_PROMPT admin setting) is the system message
    VERBATIM; the seed is the user turn. No chat state is read at all — see the
    no-history INVARIANT in the module docstring.

    # WHY: always returns a non-empty prompt — never raises. On any failure
    # (no LLM configured, LLM error, unparseable JSON) it logs a warning and falls
    # back to the raw seed. The seed is always a valid (if less polished) prompt,
    # so generation proceeds.
    #
    # WHY: the refinement model is
    # COMFYUI_PROMPT_MODEL, ALWAYS — the chat's model is NOT an input here.
    # Why: this SUPERSEDES the multi-image Part B rule that the chat-selected
    # model always wins. Measured on the same real payload, a reasoning model
    # took 164s (~3900 hidden reasoning tokens for a 170-token answer) where the
    # chat model took 4.4s; `reasoning_effort` is ignored by the router, so it
    # cannot be told to stop. That blew the turn budget (TURN_PROGRESS_GRACE_S=300
    # minus the COMFYUI_TIMEOUT_S=120 generation deadline), so every refinement
    # timed out and every image rendered from the raw seed. Rewriting a
    # description into SD phrasing has nothing to reason about; it wants a fast
    # dedicated model, not whichever model the user is chatting with.
    # COMFYUI_PROMPT_MODEL defaults to CHAT_MODEL, so this needs no new config.
    #
    # WHY: every branch returns the prompt
    # AND its outcome (_RefineResult). A fallback is no longer silent: it carries
    # ok=False + a short cause, which the driver renders as a FAILED refiner chip.
    # D6 stands (the seed still reaches ComfyUI); what changes is the user can now
    # tell a fallback from a real refinement (no-silent-degradation). Each failure
    # branch supplies a DISTINCT cause so the chip names what actually went wrong.
    """
    # The model is its own fallback chain (COMFYUI_PROMPT_MODEL ← CHAT_MODEL),
    # re-derived per refinement so a settings row on either link reaches the
    # refiner.
    api_url = await settings.get("AI_API_URL")
    model = await settings.get("COMFYUI_PROMPT_MODEL")
    if not (api_url and model):
        cause = "refinement LLM not configured"
        logger.warning("comfy: %s; using raw prompt", cause)
        return _RefineResult(prompt=seed, ok=False, error=cause)

    try:
        api_key = await settings.get("AI_API_KEY")
        prompt_timeout_s = await settings.get("COMFYUI_PROMPT_TIMEOUT_S")
        client = http_clients.get_http_client(
            "comfy_prompt", timeout=prompt_timeout_s)
        resp = await client.post(
            f"{api_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": template},
                    {"role": "user", "content": seed},
                ],
                "stream": False,
                # WHY: temperature 0. Visual diversity already comes from the
                # ComfyUI {random} seed resolution (ComfyUI does not randomize a
                # seed passed in the graph — our resolver does), so sampling here
                # buys no variety and only costs reproducibility of edits: the
                # same description must yield the same SD prompt. Matches
                # pipeline/extractor/utils.
                "temperature": 0,
                # Force schema-valid JSON {"prompt": "<string>"} (validation-by-
                # construction; see _REFINE_PROMPT_SCHEMA).
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "comfy_prompt",
                        "strict": True,
                        "schema": _REFINE_PROMPT_SCHEMA,
                    },
                },
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        # WHY type(exc).__name__: `%s` of an httpx.ReadTimeout renders as the empty
        # string, which is exactly how this stayed invisible for so long (the
        # operator saw no cause, the chip showed the seed as if refined). Naming the
        # type makes the cause legible in BOTH the log and the refiner chip.
        cause = f"refinement LLM call failed: {type(exc).__name__}"
        logger.warning("comfy: %s; using raw prompt: %r", cause, exc)
        return _RefineResult(prompt=seed, ok=False, error=cause)

    refined = _parse_prompt_json(content)
    if not refined:
        cause = "refinement produced no usable prompt"
        logger.warning(
            "comfy: %s; using raw seed (sample=%r)", cause, (content or "")[:200],
        )
        return _RefineResult(prompt=seed, ok=False, error=cause)
    return _RefineResult(prompt=refined, ok=True, error=None)
