"""Sandbox REST routes — user-facing workspace status/restart/destroy + chat attachments.

# SYSTEM: agent-sandbox — the user-facing lifecycle controls for the agent's console
#   workspace. The workspace ops themselves live in routes/tool_api/sandbox/; these
#   are SESSION routes (cookie auth, a human clicking), kept off the tool-api router
#   (the agent's key-authenticated surface) so the agent has no destroy privilege.
#   Chat attachments enter the workspace here too: a person drops the file.
"""

import asyncio
import logging
import mimetypes
import os
import posixpath
import re
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from access import require_project_full, require_project_read
from auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

# Ceiling on one chat attachment. Read at call time (tests pin it low).
CHAT_ATTACHMENT_MAX_MB = 100
_RAR_MAGIC = b"Rar!"
# Byte budget for the stored leaf name — below the 255-byte filesystem limit,
# counted in UTF-8 because a Cyrillic name is two bytes per letter.
_ATTACHMENT_NAME_MAX_BYTES = 200


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

    Shared by every route here: workspace ops are
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


def _attachment_leaf(original: str) -> str:
    """The stored file name: the client's base name, folded to a single safe leaf.

    Same fold as reference uploads (files_util.save_upload), plus the base name
    taken first and leading dots stripped, so `../x`, `a/b` and `..` can never
    name anything but a file inside the upload's own uuid dir.
    """
    name = re.sub(r"[^\w.\-]", "_", posixpath.basename((original or "").replace("\\", "/")))
    name = name.lstrip(".")
    stem, ext = os.path.splitext(name)
    budget = _ATTACHMENT_NAME_MAX_BYTES - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:max(budget, 1)].decode("utf-8", "ignore")
    return f"{stem}{ext}" if stem else (f"file{ext}" if ext else "file")


def _attachment_mime(name: str, claimed: str | None) -> str:
    """The browser's type when it named one, else a guess from the extension."""
    if claimed and claimed != "application/octet-stream":
        return claimed
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _refuse_attachment_type(name: str, head: bytes = b"") -> None:
    """415 for the one kind the chat does not take: rar.

    # WHY a denylist: the agent works on the file with its own tools (it installs
    # what it needs in the sandbox), so anything it could plausibly open is
    # accepted — video included, bounded by the size cap alone; rar is refused
    # for having no free unpacker.
    """
    if name.lower().endswith(".rar") or head.startswith(_RAR_MAGIC):
        raise HTTPException(status_code=415, detail="RAR files are not accepted")


@router.post("/api/projects/{project_id}/sandbox/attachments")
async def upload_chat_attachment(
    project_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Put a file dropped on the AI chat into the caller's sandbox workspace.

    Streams to a temp file and from there over SFTP — never the whole file in
    memory. Lands at `inbox/chat/{uuid}/{name}`; the uuid is minted here, so two
    drops of the same name never collide. Returns {id, name, size, mime, path}.
    """
    from files_util import stream_upload_to_tmp

    from routes.tool_api.sandbox.lifecycle import put_chat_attachment
    from routes.tool_api.sandbox.transport import _require_sandbox_configured

    await require_project_full(project_id, user)
    await _require_sandbox_configured()
    name = _attachment_leaf(file.filename or "")
    mime = _attachment_mime(name, file.content_type)
    _refuse_attachment_type(name)
    max_mb = CHAT_ATTACHMENT_MAX_MB
    tmp, head = await stream_upload_to_tmp(file, max_mb * 1024 * 1024, max_mb)
    try:
        _refuse_attachment_type(name, head)
        upload_id = str(uuid4())
        size = (await asyncio.to_thread(tmp.stat)).st_size
        path = await _run_sandbox_op(
            put_chat_attachment(await _sandbox_target_user(user), upload_id, name, str(tmp)),
            op_name="attachment upload",
        )
    finally:
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    return {"id": upload_id, "name": name, "size": size, "mime": mime, "path": path}


@router.delete("/api/projects/{project_id}/sandbox/attachments/{upload_id}")
async def delete_chat_attachment(
    project_id: str,
    upload_id: UUID,
    user: dict = Depends(get_current_user),
):
    """Remove an attachment dropped and then taken back before the send."""
    from routes.tool_api.sandbox.lifecycle import remove_chat_attachment

    await require_project_full(project_id, user)
    await _run_sandbox_op(
        remove_chat_attachment(await _sandbox_target_user(user), str(upload_id)),
        op_name="attachment delete",
    )
    return {"success": True}
