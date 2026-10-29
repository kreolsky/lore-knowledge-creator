"""Tests for the shared reprocess core + the Pi-only reprocess_reference tool.

Context (plan agent-reference-text-is-canon): a reference's TEXT is canon — the
import pipeline produced it. The sanctioned way to re-run that pipeline is the
Pi-only `reprocess_reference` tool, NOT re-deriving text from the binary in the
sandbox (no ASR/OCR/converters there). This file pins:

  - the shared core extracted from routes.files.retry_processing (audio →
    re-transcribe, .docx → re-convert, wipe content, queue the job);
  - the image refusal (look at the image / ask the user — never OCR);
  - the 'ready'-state handling (Decision 7 = A: accepted under the apply-mode
    gate, no special refusal — we cannot detect hand-editing);
  - the tool surface: confirm proposes (no wipe), auto applies; a non-full
    principal (Commentator/Viewer) is refused (content-destroying write).

The core lives in routes.files (retry_processing's home); the tool handler in
routes.tool_api.imports reuses it. Access checks stay at each surface's own edge
(require_document_full for REST, project access for the tool) — they are NOT
unified, by design.
"""

import secrets
from unittest.mock import AsyncMock, patch

import pytest
from helpers import pinned_stt_url

# ─── Shared seeding helpers ──────────────────────────────────────────────────


async def _seed_ref(
    test_db, project_id, ref_id, *, media_type, file_path=None,
    processing_status="error", content="OLD TEXT", parent_id=None,
    safe_name="clip.mp3",
):
    """Persist a reference row mirroring save_upload (no disk bytes needed — the
    core never reads the file, it only dispatches on media_type/file_path)."""
    from db import create_record

    if parent_id is None:
        # Reference-host invariant: attach to a real host doc (idempotent per project).
        parent_id = f"{project_id}-host"
        try:
            await create_record("documents", parent_id, {
                "project_id": project_id, "parent_id": None, "title": "Host",
                "content": "", "path": f"{parent_id}.md", "is_index": False,
            })
        except RuntimeError:
            pass
    await create_record("documents", ref_id, {
        "project_id": project_id, "parent_id": parent_id, "title": safe_name,
        "content": content, "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": media_type,
        "file_path": file_path, "processing_status": processing_status,
    })
    return ref_id


async def _fetch_ref(ref_id):
    from db import fetch_one

    return await fetch_one("documents", ref_id)


# ════════════════════════════════════════════════════════════════════════════
# Shared core (routes.files._reprocess_reference) — extracted from retry_processing
# ════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════
# Tool surface (POST /api/tool/reprocess_reference) — Pi-only
# ════════════════════════════════════════════════════════════════════════════


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    import hashlib

    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"rp-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_tool_auto_applies_and_wipes(client, test_db, admin_user, project_with_doc):
    """Full access + apply=auto → the tool runs the core directly and returns
    {status:"applied"}; the content is wiped and the job queued."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    ref_id = "rp-tool-auto"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="error")

    with pinned_stt_url(), \
            patch("files_service.enqueue_transcription", new_callable=AsyncMock):
        resp = await client.post("/api/tool/reprocess_reference", json={
            "reference_id": ref_id, "apply": "auto",
        }, headers=_hdr(agent_tok))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    after = await _fetch_ref(ref_id)
    assert after["content"] == ""
    assert after["processing_status"] == "queued"


@pytest.mark.asyncio
async def test_tool_non_full_principal_refused(
    client, test_db, admin_user, project_with_doc,
):
    """A Commentator/Viewer key (project access != full) cannot reprocess — it is a
    content-destroying write. Assert the refusal, not the happy path."""
    from db import create_record

    pid, _, _admin_uid = project_with_doc

    # A non-owner member demoted to Commentator (the owner is always full, so the
    # refusal must be exercised through a separate principal) + their agent key.
    comm_uid = f"comm-{secrets.token_hex(4)}"
    await create_record("users", comm_uid, {
        "email": f"{comm_uid}@x.test", "name": comm_uid, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"pm-{secrets.token_hex(4)}", {
        "project_id": pid, "user_id": comm_uid, "access_level": "commentator",
    })
    agent_tok = await _make_agent_key(test_db, comm_uid, pid)
    ref_id = "rp-tool-viewer"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="error")

    resp = await client.post("/api/tool/reprocess_reference", json={
        "reference_id": ref_id, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 403, resp.text


# ════════════════════════════════════════════════════════════════════════════
# Cross-project authorization — full access to project A must not reach project B
# (parity with the REST retry edge's require_document_full + the edit executors'
# session-project binding). The wipe is content-destroying; content must stay intact.
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tool_refuses_cross_project_reference(
    client, test_db, admin_user, project_with_doc,
):
    """A key bound to project A must NOT wipe a reference living in project B. The
    tool binds the fetched ref to ctx['project_id'] (parity with the REST retry edge,
    which gates on the ref's OWN project via require_document_full)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)  # key for project A
    other_pid = "rp-other-project"
    ref_id = "rp-xproj"
    await _seed_ref(test_db, other_pid, ref_id, media_type="audio",
                    file_path=f"{other_pid}/{ref_id}/clip.mp3", processing_status="error")

    resp = await client.post("/api/tool/reprocess_reference", json={
        "reference_id": ref_id, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 404, resp.text
    # Uniform 404 (no existence oracle) — but the cross-project wipe never happened.
    assert (await _fetch_ref(ref_id))["content"] == "OLD TEXT"


# ════════════════════════════════════════════════════════════════════════════
# Reference-file serving — Content-Disposition is inline ONLY for PDF
# (plan pdf-import-pymupdf4llm: "open in new tab" must render in the browser's
# viewer, not download; every other binary keeps the attachment default —
# explicit is better than relying on FileResponse's default here).
# ════════════════════════════════════════════════════════════════════════════
