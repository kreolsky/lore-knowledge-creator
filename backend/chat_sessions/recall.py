"""chat-recall: the skill's name (its off-switch key) and the index-reconcile trigger shared by every writer.

# SYSTEM: chat-recall — search over the user's own other chats (off-switch, reindex trigger)
#   Names the admin off-switch (read through agent_config_load.instance_skill_disabled)
#   and owns the reconcile trigger every chat writer posts;
#   the chat_index table is built by jobs/tasks/chat_index.py, the
#   visibility-checked read path is chat_sessions/recall_search.py. The same
#   trigger posts the chat-topics reconcile (jobs/tasks/chat_topics.py), so
#   every writer that changes what the index sees also re-checks the tocs.
"""
import logging
from datetime import timedelta

from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)

CHAT_RECALL_SKILL = "chat-recall"
# A chat whose unlabelled tail has sat this long since its last message is
# labelled even below the close-every count; the turn-end idle check fires one
# minute after it. Lives here, not in jobs/tasks/chat_topics.py: that module
# imports this one.
CHAT_TOPICS_IDLE_MIN = 3
# The gap before a run that spent its whole call budget continues.
CHAT_TOPICS_MORE_DEFER = timedelta(seconds=60)


async def schedule_chat_index(
    user_id: str, project_id: str, turn_end_msg_id: str | None = None,
) -> None:
    """Post the (user, project) reconcile — the chat index AND the chat topics;
    a pending one absorbs this post. `turn_end_msg_id` (the turn-end caller
    only) also posts the deferred idle check, keyed by that assistant message
    so turn ends never collapse onto each other.

    WHY: a failed post is logged, never raised — the caller is a user write
    that already succeeded, and the hourly sweeps reconcile whatever a lost
    post left behind.
    """
    if not user_id or not project_id:
        return
    await _post_reconcile("index_chat_messages", user_id, project_id,
                          f"chat_index:{user_id}:{project_id}")
    await schedule_chat_topics(user_id, project_id)
    if turn_end_msg_id:
        await _post("chat_topics_idle_task", user_id, project_id,
                    job_id=f"chat_topics_idle:{turn_end_msg_id}",
                    defer=timedelta(minutes=CHAT_TOPICS_IDLE_MIN + 1))


async def schedule_chat_topics(user_id: str, project_id: str) -> None:
    """Post the (user, project) chat-topics reconcile alone (the idle check and
    the topics sweep — neither has index work to post)."""
    await _post_reconcile("chat_topics_pair_task", user_id, project_id,
                          f"chat_topics:{user_id}:{project_id}")


async def schedule_chat_topics_more(user_id: str, project_id: str) -> None:
    """Post the continuation of a run that spent its call budget: the idle task
    (it only re-posts the pair reconcile), deferred, under its own job id.

    WHY not the pair task itself: arq holds the pair task's id while it runs,
    so a post from inside the run would collapse onto the running job.
    """
    await _post("chat_topics_idle_task", user_id, project_id,
                job_id=f"chat_topics_more:{user_id}:{project_id}",
                defer=CHAT_TOPICS_MORE_DEFER)


def _rerun_key(job_id: str) -> str:
    return f"lore:rerun:{job_id}"


def _topics_first_key(user_id: str, project_id: str) -> str:
    return f"lore:topics_first:{user_id}:{project_id}"


async def mark_topics_first(user_id: str, project_id: str, chat_ids: set[str]) -> None:
    """Ask the pair's next label passes to take `chat_ids` before the newest-first order.

    WHY a failure only warns: the ids are an ordering hint — without them the
    chats are still labelled, newest first.
    """
    if not chat_ids:
        return
    try:
        pool = await jobs_pool.get_arq_pool()
        key = _topics_first_key(user_id, project_id)
        await pool.sadd(key, *sorted(chat_ids))
        await pool.expire(key, 3600)
    except Exception:
        logger.warning("chat topics priority mark failed user=%s project=%s",
                       user_id, project_id, exc_info=True)


async def take_topics_first(ctx: dict, user_id: str, project_id: str) -> set[str]:
    """Pop the pair's priority ids (empty outside arq or on a Redis failure)."""
    redis = ctx.get("redis")
    if redis is None:
        return set()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.smembers(_topics_first_key(user_id, project_id))
            pipe.delete(_topics_first_key(user_id, project_id))
            members, _ = await pipe.execute()
    except Exception:
        logger.warning("chat topics priority take failed user=%s project=%s",
                       user_id, project_id, exc_info=True)
        return set()
    return {m.decode() if isinstance(m, bytes) else str(m) for m in members or ()}


async def _post_reconcile(function_name: str, user_id: str, project_id: str, job_id: str) -> None:
    """Post a (user, project) reconcile; a post arq collapses leaves a rerun flag.

    INVARIANT(security): a reconcile post is never lost to a RUNNING reconcile
    of the same pair. Why: arq holds the job id until the run finishes, so the
    post a «Не запоминать» toggle makes mid-run would collapse, and the hidden
    chat's toc / chunks would outlive the toggle until the hourly sweep. The
    running task consumes the flag (`take_rerun`) and passes again.
    """
    if await _post(function_name, user_id, project_id, job_id=job_id):
        return
    try:
        pool = await jobs_pool.get_arq_pool()
        await pool.set(_rerun_key(job_id), 1, ex=3600)
    except Exception:
        logger.warning("%s rerun flag failed user=%s project=%s",
                       function_name, user_id, project_id, exc_info=True)


async def take_rerun(ctx: dict) -> bool:
    """Consume this job's rerun flag: True when a post collapsed onto it since
    the last take. A reconcile takes it once at start (a post that collapsed
    onto the PENDING job is covered by this run) and after every pass. A call
    outside arq (no job id) holds no job a post could collapse onto: False."""
    job_id = ctx.get("job_id")
    if not job_id:
        return False
    return bool(await ctx["redis"].getdel(_rerun_key(job_id)))


async def _post(function_name: str, user_id: str, project_id: str, **opts) -> bool:
    """Post a job; True when it landed or failed (nothing a rerun flag can add),
    False when arq collapsed it onto an existing job of the same id."""
    try:
        return await jobs_pool.enqueue(function_name, user_id, project_id, **opts) is not None
    except Exception:
        logger.warning("%s enqueue failed user=%s project=%s",
                       function_name, user_id, project_id, exc_info=True)
        return True
