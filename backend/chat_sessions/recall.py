"""chat-recall: the skill's off-switch and the index-reconcile trigger shared by every writer."""
import logging

from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)

CHAT_RECALL_SKILL = "chat-recall"


async def chat_recall_disabled(db) -> bool:
    """True when the admin disabled the shipped `chat-recall` skill.

    The standard off-switch — a disabled instance_skills row of that name —
    read here by the index tasks and by the Tool-API handler, so there is no
    second switch to drift from the one the skill loader honours.
    """
    rows = await db.query(
        "SELECT enabled FROM instance_skills WHERE name = $n",
        {"n": CHAT_RECALL_SKILL},
    ) or []
    return any(r.get("enabled") is False for r in rows)


async def schedule_chat_index(user_id: str, project_id: str) -> None:
    """Post the (user, project) reconcile; a pending one absorbs this post.

    WHY: a failed post is logged, never raised — the caller is a user write
    that already succeeded, and the hourly chat_index_sweep_task reconciles
    whatever a lost post left behind.
    """
    if not user_id or not project_id:
        return
    try:
        await jobs_pool.enqueue(
            "index_chat_messages", user_id, project_id,
            job_id=f"chat_index:{user_id}:{project_id}",
        )
    except Exception:
        logger.warning("chat index enqueue failed user=%s project=%s",
                       user_id, project_id, exc_info=True)
