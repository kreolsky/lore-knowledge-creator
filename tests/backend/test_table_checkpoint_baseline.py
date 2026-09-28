"""Tests for the checkpoint baseline-form fix (Fix 2).

A loss/auto-backup baseline MUST always be the raw anchor-form text
(``![label](table:id)``) paired with the lossless ``tables_json`` — never the
GFM-derived ``documents.content`` (which inlines tables as dead pipe text). This
guards three entry points:

  1. The collab flush passes an empty-string baseline AS-IS. Coercing ``""`` to
     ``None`` (``_last_flushed_content or None``) made the worker fall into the
     ``documents.content`` (GFM) fallback, so a flush right after the buffer was
     emptied stored a GFM baseline.
  2. ``maybe_backup_on_content_loss`` None-fallback derives the baseline text +
     tables_json from the persisted Y.Doc (``capture_live_state``), not
     ``documents.content``.
  3. The REST PATCH no-collab fallback compares anchor-form (``capture_live_state``)
     against anchor-form ``body.content``, so a table-expansion-only difference is
     NOT counted as a change (no spurious backup) and a real loss backs up a true
     restore point.
"""

from unittest.mock import AsyncMock, patch

from auto_backup import maybe_backup_on_content_loss
from collab.session import CollabSession
from emit_recorder import EmitRecorder
from enqueue_recorder import EnqueueRecorder
from pycrdt import Doc, Text

from models import PatchDocument

# ─── helpers ──────────────────────────────────────────────────────────────────
GFM_TABLE = "| h |\n| --- |\n| a |\n"
TABLES_JSON = '{"abc":{"columns":[160],"rows":[["h"],["a"]]}}'


def _anchor_baseline() -> str:
    """A long anchor-form baseline (well above the loss thresholds)."""
    return "![t](table:abc) " + ("word " * 160)


# ─── 1. session flush: empty baseline not coerced to None ─────────────────────
class TestSessionBaselineNotCoerced:
    async def test_empty_flushed_content_passed_as_empty_string_not_none(self):
        """``_last_flushed_content == ""`` must reach the loss-task enqueue as
        ``baseline_content=""`` (nothing to lose), NOT ``None`` (unknown → worker
        reads the GFM ``documents.content`` fallback)."""
        doc = Doc()
        text = doc.get("content", type=Text)
        text += "hello world"
        session = CollabSession(entity_type="doc", entity_id="d1", ydoc=doc)
        session._last_flushed_content = ""  # the bug: `or None` turns this into None
        session._dirty = True

        db_mock = AsyncMock()
        db_mock.query = AsyncMock()

        with patch("collab.flush_pipeline.get_db", return_value=db_mock), \
                EnqueueRecorder.active() as mock_enqueue, \
                patch("collab.flush_pipeline.extract_doc_mentions", return_value=[]):
            await session.flush_to_db()

        assert len(mock_enqueue.calls) == 1
        assert mock_enqueue.calls[0].kwargs["baseline_content"] == ""


# ─── 2. loss backup None-fallback: derive from Y.Doc, not documents.content ───
class TestLossBackupAnchorFormBaseline:
    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_none_baseline_uses_ydoc_anchor_text_not_gfm(
        self, mock_get_db, mock_create, _mock_json, emit_recorder,
    ):
        """When no baseline was passed (``baseline_content=None``), the fallback
        MUST load the Y.Doc and store the raw anchor text + tables_json — never the
        GFM ``documents.content``."""
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1"}
        # The documents row carries the DERIVED GFM; capture_live_state carries the
        # raw anchor form. The fix must ignore the GFM and use the anchor form.
        anchor = _anchor_baseline()

        with patch("auto_backup.fetch_one", new_callable=AsyncMock,
                   return_value={"is_reference": False, "content": GFM_TABLE}), \
                patch("ydoc_store.capture_live_state", new_callable=AsyncMock,
                      return_value=(anchor, TABLES_JSON)) as mock_live:
            result = await maybe_backup_on_content_loss(
                "doc-anchor", "x", baseline_content=None, baseline_tables_json=None,
            )

        assert result is not None
        mock_live.assert_awaited_once_with("doc-anchor")
        mock_create.assert_called_once()
        stored = mock_create.call_args.kwargs["content"]
        assert "![t](table:abc)" in stored          # raw anchor form
        assert "| --- |" not in stored               # never the GFM expansion
        # tables_json derived from the SAME loaded Y.Doc (pairing invariant #6).
        assert mock_create.call_args.kwargs["tables_json"] == TABLES_JSON

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_reference_doc_not_backed_up_in_none_fallback(
        self, mock_get_db, mock_create, _mock_json, emit_recorder,
    ):
        """A reference edited with an unknown baseline still must NOT back up
        (references are media, not prose). The is_reference guard survives the
        switch to capture_live_state."""
        mock_db = AsyncMock()
        mock_db.query = AsyncMock()
        mock_get_db.return_value = mock_db

        with patch("auto_backup.fetch_one", new_callable=AsyncMock,
                   return_value={"is_reference": True, "content": GFM_TABLE}), \
                patch("ydoc_store.capture_live_state", new_callable=AsyncMock) as mock_live:
            result = await maybe_backup_on_content_loss(
                "doc-ref", "x", baseline_content=None,
            )

        assert result is None
        mock_create.assert_not_called()
        mock_live.assert_not_called()  # bail before loading the Y.Doc


# ─── 3. PATCH no-collab fallback: anchor-form comparison + baseline ───────────
class TestPatchAnchorFormComparison:
    async def _run_patch(self, body_content: str, live_text: str):
        from routes.documents import patch_document

        body = PatchDocument(content=body_content)
        db_mock = AsyncMock()
        db_mock.query = AsyncMock()
        with                 patch("routes.documents.require_document_full", new_callable=AsyncMock), \
                patch("documents.update.get_doc_project_id", new_callable=AsyncMock,
                      return_value="p1"), \
                patch("collab.registry.get_active_session", return_value=None), \
                patch("documents.update.fetch_one", new_callable=AsyncMock,
                      return_value={"is_reference": False, "content": GFM_TABLE}), \
                patch("ydoc_store.capture_live_state", new_callable=AsyncMock,
                      return_value=(live_text, TABLES_JSON)), \
                patch("auto_backup.maybe_backup_on_content_loss",
                      new_callable=AsyncMock) as mock_backup, \
                patch("documents.update.get_db", new_callable=AsyncMock,
                      return_value=db_mock), \
                patch("mentions.rebuild_doc_mentions",
                      new_callable=AsyncMock, return_value=[]), \
                EmitRecorder.active():
            await patch_document("doc1", body, user={"user_id": "u1"})
        return mock_backup

    async def test_table_expansion_only_difference_no_spurious_backup(self):
        """``body.content`` (anchor form) equal to the live raw text must NOT
        trigger a backup, even though ``documents.content`` (GFM) differs. The bug
        compared GFM-vs-anchor and counted the expansion as a change."""
        anchor = _anchor_baseline()
        mock_backup = await self._run_patch(body_content=anchor, live_text=anchor)
        mock_backup.assert_not_called()

    async def test_real_loss_baseline_is_anchor_form(self):
        """On a genuine loss, the baseline passed to the loss backup MUST be the
        raw anchor text (from ``capture_live_state``), not the GFM
        ``documents.content``."""
        long_anchor = _anchor_baseline()
        mock_backup = await self._run_patch(body_content="short", live_text=long_anchor)
        mock_backup.assert_called_once()
        baseline = mock_backup.call_args.kwargs["baseline_content"]
        assert baseline == long_anchor
        assert "![t](table:abc)" in baseline
        assert "| --- |" not in baseline
        assert mock_backup.call_args.kwargs["baseline_tables_json"] == TABLES_JSON
