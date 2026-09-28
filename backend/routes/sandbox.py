"""Sandbox lifecycle REST routes — user-facing workspace status/restart/destroy.

# SYSTEM: agent-sandbox — the user-facing lifecycle controls for the agent's console
#   workspace. The workspace ops themselves live in routes/tool_api/sandbox/; these
#   are SESSION routes (cookie auth, a human clicking), kept off the tool-api router
#   (the agent's key-authenticated surface) so the agent has no destroy privilege.

# ARCH: relocated from routes/projects.py (Block 2.6) + the three duplicated
#   try/except wrappers collapsed into one _run_sandbox_op helper. The sandbox
#   itself lives in routes/tool_api/sandbox/; these are the user-facing lifecycle
#   controls. They are here, not on the tool-api router, because they are SESSION
#   routes (cookie auth, a human clicking) — the tool-api router is the agent's
#   key-authenticated surface, and the agent deliberately has no destroy privilege.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from access import require_project_full, require_project_read
from auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


async def _sandbox_target_user(user: dict) -> str:
    """The workspace these routes act on: ALWAYS the caller's own.

    # INVARIANT(security): sandbox lifecycle acts on the CALLING user's workspace;
    # project_id in the path is the ACL anchor only, never a workspace selector.
    # Why: sandboxes are per-USER, not per-project (they match the identity boundary
    # the agent already has), so honoring a project-wide reading would let a project
    # Owner wipe a colleague's files — separation between users is a convention here,
    # and this route must not be the thing that breaks it.
    """
    return user["user_id"]


async def _run_sandbox_op(coro, *, op_name: str):
    """Run a sandbox workspace op, mapping non-HTTP failures to a 502.

    Shared by the three lifecycle routes (Block 2.6 dedup): workspace ops are
    SSH-driven, so a transport/SSH failure is 'Sandbox unreachable' (502), while an
    intentional HTTPException (e.g. a guard) passes through unchanged.
    """
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("sandbox %s failed: %s", op_name, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Sandbox unreachable: {exc}") from exc


@router.get("/api/projects/{project_id}/sandbox")
async def get_sandbox_status(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    """Disk usage + whether the caller's workspace exists."""
    from routes.tool_api.sandbox.lifecycle import workspace_status

    await require_project_read(project_id, user)
    return await _run_sandbox_op(
        workspace_status(await _sandbox_target_user(user)), op_name="status",
    )


@router.post("/api/projects/{project_id}/sandbox/restart")
async def restart_sandbox(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    """Reset the caller's sandbox environment, keeping their files.

    Guarded by full project access: 'full' is Owner/Editor in this codebase's
    vocabulary. Commentator/readonly are rejected — resetting a work environment is
    not a commenting privilege.
    """
    from routes.tool_api.sandbox.lifecycle import restart_workspace

    await require_project_full(project_id, user)
    await _run_sandbox_op(
        restart_workspace(await _sandbox_target_user(user)), op_name="restart",
    )
    return {"success": True}


@router.post("/api/projects/{project_id}/sandbox/destroy")
async def destroy_sandbox(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    """Delete the caller's workspace, files included. Not reversible."""
    from routes.tool_api.sandbox.lifecycle import destroy_workspace

    await require_project_full(project_id, user)
    await _run_sandbox_op(
        destroy_workspace(await _sandbox_target_user(user)), op_name="destroy",
    )
    return {"success": True}
