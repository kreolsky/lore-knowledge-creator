"""The digest binding at run time — its agent_configs row and whose chats THIS run reads."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from access import get_project_access, resolve_digest_subjects
from db import extract_id, fetch_one


@dataclass(frozen=True)
class DigestBinding:
    config_id: str
    project_id: str
    theme_doc_id: str
    config_doc_id: str
    target_doc_id: str
    created_by: str
    subjects: tuple[str, ...]
    title_template: str | None
    model: str | None


async def _project_access(project_id: str, user_id: str) -> tuple[dict | None, str | None]:
    user = await fetch_one("users", user_id)
    if not user:
        return None, None
    caller = {"user_id": user_id, "role": user.get("role")}
    return caller, await get_project_access(project_id, caller)


async def run_subjects(project_id: str, created_by: str, stored: list[str] | None) -> tuple[str, ...]:
    """The subjects this run may read: the stored list minus users who lost project
    access, re-validated against the creator as of NOW.

    Raises PermissionError when the creator lost access, or when the list still
    names someone other than the creator and the creator is no longer root.
    """
    caller, access = await _project_access(project_id, created_by)
    if access is None:
        raise PermissionError("The digest's creator no longer has access to this project")
    kept = [uid for uid in dict.fromkeys(stored or [created_by])
            if (await _project_access(project_id, uid))[1] is not None]
    # WHY: an empty list stays empty — resolve_digest_subjects reads None/[] as
    # "the creator", which would silently swap a departed member's chats for the creator's.
    if not kept:
        return ()
    # INVARIANT(security): the subject gate is re-run with the creator as caller on every run, not only at write time.
    # Why: a creator who stopped being root must not keep copying other members' chats into a shared document.
    try:
        return tuple(await resolve_digest_subjects(project_id, caller, created_by, kept))
    except HTTPException as e:
        raise PermissionError(e.detail) from e


async def load_binding(config_id: str, project_id: str) -> DigestBinding:
    """The live digest row `config_id` of `project_id`, its subjects rechecked.

    Raises ValueError for a missing / foreign / non-digest row or a dead theme or
    target document, PermissionError from run_subjects.
    """
    row = await fetch_one("agent_configs", config_id)
    if not row or row.get("deleted_at") or row.get("kind") != "digest":
        raise ValueError(f"Digest binding {config_id} not found")
    if extract_id(row.get("project_id")) != project_id:
        raise ValueError(f"Digest binding {config_id} does not belong to project {project_id}")
    created_by = row.get("created_by")
    if not created_by:
        raise ValueError(f"Digest binding {config_id} has no creator")
    theme_doc_id = extract_id(row.get("document_id"))
    target_doc_id = extract_id(row.get("target_doc_id"))
    for doc_id in (theme_doc_id, target_doc_id):
        doc = await fetch_one("documents", doc_id)
        if not doc or doc.get("deleted_at") or doc.get("project_id") != project_id:
            raise ValueError(f"Digest document {doc_id} not found in project {project_id}")
    return DigestBinding(
        config_id=config_id,
        project_id=project_id,
        theme_doc_id=theme_doc_id,
        config_doc_id=extract_id(row.get("config_doc_id")),
        target_doc_id=target_doc_id,
        created_by=created_by,
        subjects=await run_subjects(project_id, created_by, row.get("subject_user_ids")),
        title_template=row.get("title_template"),
        model=row.get("model"),
    )
