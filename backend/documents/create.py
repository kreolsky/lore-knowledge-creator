"""Doc-command create — create_document_command (title/path derivation + row create).

Subsystem overview and ARCH notes live in documents/__init__.py.
See SYSTEM: documents (entry: backend/documents/__init__.py).
"""

import logging
from uuid import uuid4

from fastapi import HTTPException

import event_bus
from db import fetch_one, serialize_record
from documents.service import (
    assert_parent_valid,
    create_document,
    create_with_unique_path,
    generate_unique_title,
)
from models import CreateDocument

logger = logging.getLogger(__name__)


async def _derive_title_path(body: CreateDocument) -> tuple[str | None, str | None, bool]:
    """Resolve (title, path, auto_path) per the create rules.

    auto_path: path is derived from the title (collision-prone under concurrent
    same-title creates). When set, creation routes through create_with_unique_path
    which re-derives the slug on a UNIQUE collision (shared with the agent path).
    """
    title = body.title
    path = body.path
    auto_path = False
    if body.is_reference:
        # Reference paths live under the _ref/ namespace so they don't clash with
        # user-created documents in the path UNIQUE index.
        path = path or f"_ref/{uuid4()}.md"
        title = title or "Untitled"
    elif not title:
        title, path = await generate_unique_title(body.project_id)
    elif not path:
        auto_path = True
    return title, path, auto_path


async def _create_row(
    doc_id: str, payload: dict, *, auto_path: bool, project_id: str,
    title: str | None, user_id: str | None, user_name: str | None,
) -> None:
    """Dispatch to the creation primitive, mapping failures to HTTP errors."""
    try:
        # create_document assigns the newest-first sort_key for non-reference docs.
        if auto_path:
            await create_with_unique_path(
                doc_id, payload, project_id=project_id, title=title,
                user_id=user_id, user_name=user_name,
            )
        else:
            await create_document(doc_id, payload, user_id=user_id, user_name=user_name)
    except RuntimeError:
        raise HTTPException(status_code=400, detail="Failed to create document")
    except Exception:
        logger.exception("Unexpected error creating document")
        raise HTTPException(status_code=500, detail="Internal error")


async def create_document_command(
    body: CreateDocument, *, user_id: str | None, user_name: str | None,
) -> dict:
    """Create a new document; title and path auto-generated if omitted.

    Reference-creation rules (media_type requirement, _ref path namespace):
    the create_document_endpoint docstring in routes/documents.py.
    """
    await assert_parent_valid(body.parent_id, body.project_id)
    if body.is_reference and not body.media_type:
        raise HTTPException(status_code=400, detail="Reference documents require media_type")

    title, path, auto_path = await _derive_title_path(body)
    doc_id = str(uuid4())
    payload: dict = {
        "project_id": body.project_id,
        "parent_id": body.parent_id,
        "title": title,
        "content": body.content or "",
        "path": path,
        "is_index": False,
        "is_reference": body.is_reference,
    }
    if body.is_reference:
        payload["media_type"] = body.media_type
        if body.source_url:
            payload["source_url"] = body.source_url
    await _create_row(
        doc_id, payload, auto_path=auto_path, project_id=body.project_id,
        title=title, user_id=user_id, user_name=user_name,
    )
    await event_bus.emit("document_created", project_id=body.project_id, document_id=doc_id,
                         title=title, parent_id=body.parent_id, is_reference=body.is_reference,
                         sort_key=payload.get("sort_key"))
    record = await fetch_one("documents", doc_id)
    return serialize_record(record, "document_id") if record else {
        "document_id": doc_id,
        "project_id": body.project_id,
        "title": title,
        "content": body.content or "",
        "path": path,
        "parent_id": body.parent_id,
        "is_reference": body.is_reference,
    }
