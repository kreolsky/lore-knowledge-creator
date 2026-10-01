"""Image generation persist — references + chips + the settled push.

Subsystem overview: image_generation/__init__.py (see SYSTEM: comfy-image-gen).
save_upload, the _persist_gen_steps BOLA-gated chip write, the server-side chip
builders and the settled chat-frame push call RESOLVE here
(image_generation.persist.* — including `image_generation.persist._push_gen_settled`,
the binding _persist_and_announce imports from .events).
"""

import logging

from files_util import IMAGE_EXT_BY_MIME, save_upload

from image_generation.image_refine import _RefineResult

from .events import (
    _persist_gen_steps,
    _push_gen_settled,
)

logger = logging.getLogger(__name__)

def _gen_call_ids(run_id: str) -> tuple[str, str]:
    """Stable tool_call_ids for the detached generation's two chips, keyed by
    run_id (the dispatching agent call id rides the generate_image step's own
    `call_id`). The refiner chip precedes the generation chip; both share the
    run_id so the live `done` handler and reload render them consistently."""
    gen = f"gen:{run_id}"
    return f"{gen}:refine", gen

async def _save_image_references(
    ctx: dict, run_id: str, target_doc_id: str, title: str,
    outputs: list[tuple[bytes, str]],
) -> list[str]:
    """Persist each generated output as an image reference via save_upload
    (which enqueues thumbnails + emits reference_created); returns the ref ids.

    The byline resolves through the ONE shared chain (api_key_auth.agent_author_name:
    key label → user_name → user.name) — created_by (rights axis) stays
    ctx["user_id"]."""
    from api_key_auth import agent_author_name

    created_by = ctx.get("user_id") or None
    created_by_name = agent_author_name(ctx)
    reference_ids: list[str] = []
    for i, (data, mime) in enumerate(outputs):
        ext = IMAGE_EXT_BY_MIME[mime]
        ref_id, _result = await save_upload(
            data, mime, f"{run_id}_{i}.{ext}", ctx["project_id"],
            target_doc_id, title=title, media_type="image", processing_status=None,
            created_by=created_by, created_by_name=created_by_name,
        )
        reference_ids.append(ref_id)
    return reference_ids

async def _persist_and_announce(
    ctx: dict, run_id: str, target_doc_id: str, title: str, message_id: str | None,
    refine: _RefineResult, outputs: list[tuple[bytes, str]],
) -> None:
    """Persist each output as an image reference (save_upload enqueues thumbnails +
    emits reference_created), build the refiner + image chips server-side and write
    them to `messages.gen_steps` (BEFORE the settled push so a reload always shows
    them), then push the run's settled `lore/image-gen` frame onto the owner's
    chat channel — minted from the payload anchor through the SAME builder the
    reload uses. ARCH: the refined SD prompt is never returned to the agent — it
    reaches the chat inside the minted frame and the persisted chip, which derive
    from the same step dicts."""
    # Agent-driven creation IS attributed (plan reference-card-author-nickname rule 4):
    # the key-owning human is the author. user_name rides the rebuilt worker ctx;
    # the web/test path resolves it from the full agent ctx's user dict.
    # S1: a making key with a LABEL (external agent key) takes the byline instead —
    # key_label rides both the web ctx and the worker payload's rebuilt ctx, so this
    # one resolution chain covers both paths. created_by (rights axis) is unchanged.
    reference_ids = await _save_image_references(ctx, run_id, target_doc_id, title, outputs)
    refine_call_id, gen_call_id = _gen_call_ids(run_id)
    refine_step = {
        "tool_call_id": refine_call_id,
        "tool": "refine_prompt",
        "summary": "refine prompt",
        "detail": (f"{refine.error or ''}\n\n{refine.prompt}" if not refine.ok else refine.prompt),
    }
    if not refine.ok:
        refine_step["outcome"] = "failed"
    steps = [refine_step, {
        "tool_call_id": gen_call_id,
        "tool": "generate_image",
        "summary": "generate image",
        "image_ref_ids": reference_ids,
        "run_id": run_id,
        # The dispatching dsh call id — the ONLY anchor the card mint uses
        # (driver.frames._image_run_anchor_index, both the reload attach and
        # the worker's live mint over the replay).
        "call_id": ctx.get("call_id"),
        # The target document title rides the chip so the lore/image-gen mint
        # (driver.frames) carries it on BOTH paths — the live frame and the
        # reload re-derivation are the same bytes from the same step dicts.
        "title": title,
    }]
    await _persist_gen_steps(ctx, message_id, steps)
    await _push_gen_settled(ctx, run_id, steps)
    logger.info("comfy: generate SUCCEEDED run_id=%s refs=%s", run_id, reference_ids)


async def _announce_failure(
    ctx: dict, run_id: str, message_id: str | None, error: str,
) -> None:
    """Persist the failure chip to `messages.gen_steps`, then push the run's
    SETTLED FAILED frame onto the owner's chat channel (minted from the same
    step dicts the reload renders) — a failed run shows its card live.

    # ARCH: the failed detached run is persisted for the same reason the
    # successful one is — it finished in a background task the driver's log never
    # saw, so without this column a reload shows an eternal "running…" plate for a
    # generation that died minutes ago.
    """
    _refine_call_id, gen_call_id = _gen_call_ids(run_id)
    steps = [{
        "tool_call_id": gen_call_id,
        "tool": "generate_image",
        "summary": "generate image",
        "detail": error,
        "outcome": "failed",
        "run_id": run_id,
        "call_id": ctx.get("call_id"),
    }]
    await _persist_gen_steps(ctx, message_id, steps)
    await _push_gen_settled(ctx, run_id, steps)