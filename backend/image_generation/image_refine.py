"""Prompt-refinement primitives (agent-only image gen).

Pure leaf of image_generation: the refinement LLM's JSON schema
(_REFINE_PROMPT_SCHEMA), the prompt-field extractor (_parse_prompt_json), and the
_RefineResult outcome. _refine_prompt itself lives in image_generation/refine.py —
it resolves the chat line and the refinement model through the settings chains at
call time (tests pin the config.* fallback bucket) + the shared pool's
"comfy_prompt" client (SYSTEM: http-clients).
"""
import json
import re
from typing import NamedTuple


class _RefineResult(NamedTuple):
    """Outcome of one refinement attempt.

    `prompt` is what reaches ComfyUI — the refined prompt on success, the raw seed
    on any fallback (D6: generation never blocks on a refinement miss). `ok` is
    False on every fallback so the driver renders a FAILED refiner chip naming the
    cause (no-silent-degradation); a green chip carrying the seed is exactly the
    silent failure this replaces. `error` is a short human-readable cause (None on
    success). Carried to the driver as the return value (the run is in-process now —
    the former Redis stash it described is gone)."""
    prompt: str
    ok: bool
    error: str | None



_PROMPT_JSON = re.compile(r"\{.*\}", re.DOTALL)

# Structured-Outputs schema for the refinement LLM call: forces the model to
# return {"prompt": "<string>"} (compiled to a GBNF grammar on the llama-server
# backend — the SAME mechanism as pipeline/extractor/utils.call_llm_structured,
# against the same AI_API_URL/CHAT_MODEL the refinement defaults to). This is
# validation-by-construction for the SD prompt: the model cannot emit prose,
# fences, or a wrong shape. _parse_prompt_json still tolerates a prose-wrapped
# fallback for non-compliant providers, and the D6 seed-fallback covers any
# remaining miss — so a schema/prompt failure degrades to a still-valid prompt,
# never blocks generation.
_REFINE_PROMPT_SCHEMA: dict = {
    "type": "object",
    "properties": {"prompt": {"type": "string"}},
    "required": ["prompt"],
    "additionalProperties": False,
}



def _parse_prompt_json(text: str) -> str | None:
    """Extract the `prompt` field from the model's JSON output.

    Prefers a direct parse (the structured-output path returns clean JSON), then
    falls back to a tolerant regex extraction for providers that wrap JSON in
    prose. Returns None when there is no non-empty `prompt` string — the caller
    then falls back to the raw seed.
    """
    obj: object = None
    try:
        obj = json.loads(text or "")
    except (json.JSONDecodeError, TypeError):
        m = _PROMPT_JSON.search(text or "")
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    p = obj.get("prompt")
    return p.strip() if isinstance(p, str) and p.strip() else None
