"""ComfyUI HTTP constants + the generation exception (agent-only image gen).

Pure leaf of image_generation: the per-request timeout constants for the shared
pool clients ("comfy" / "comfy_prompt", SYSTEM: http-clients) and the _GenError
that every failure path raises. The gated-config guard is NOT here (it reads a
monkeypatched config — it lives with the launcher in routes/tool_api/image_gen).
"""
import httpx

# Per-request timeout (review fix m2): short, applies to EVERY ComfyUI HTTP call.
# /prompt only enqueues (returns at once), /history polls are fast, /view is a
# single image read — so a 30s read bound is plenty and keeps the worst case
# inside TURN_TIMEOUT_S=300.
_PER_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)



class _GenError(Exception):
    """A background-generation failure carrying an HTTP-style (status, detail) so
    run_generation can map the former inline HTTPException raises onto the
    generate_image_failed event without a live request context. Never escapes
    run_generation (it is caught and emitted as a failure)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
