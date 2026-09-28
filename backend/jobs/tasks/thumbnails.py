"""Thumbnail task — see SYSTEM: thumbnails."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def thumbnail_task(ctx, project_id: str, ref_id: str, rel_path: str) -> None:
    from thumbnails import generate_thumbnail, get_thumb_path, warm_model_variant

    from config import STORAGE_PATH as _SP

    abs_path = (_SP / rel_path).resolve()
    if not abs_path.is_file():
        logger.warning("Thumbnail task: source file missing: %s", rel_path)
        return
    thumb_path = get_thumb_path(project_id, ref_id)
    await asyncio.to_thread(generate_thumbnail, abs_path, thumb_path)
    # Pre-warm the model-bound variant beside the thumbnail (best-effort; mime is
    # detected from the image since the job carries no stored metadata).
    await asyncio.to_thread(warm_model_variant, abs_path, project_id, ref_id)
    logger.info("Thumbnail generated for ref %s", ref_id)
