"""Tests for the content-loss gate-2 fix — whitespace-only reformatting must not trigger.

The span-based heuristic (first…last diff char) over-counts scattered indent changes and
fired spurious 41 KB backups on whitespace-only reformats (doc 9b73b302). The fix replaces
it with actual changed-character counting via difflib + a whitespace-only fast-path guard.
"""

from unittest.mock import AsyncMock, patch

from auto_backup import maybe_backup_on_content_loss


def _make_content(paragraphs: list[str], indent: str = "    ") -> str:
    return "\n".join(f"{indent}{p}" for p in paragraphs)


def _reindent(content: str, old_indent: str, new_indent: str) -> str:
    lines = content.split("\n")
    return "\n".join(
        (new_indent + line.removeprefix(old_indent)) if line.startswith(old_indent) else line
        for line in lines
    )


def _paragraphs_for_41k() -> list[str]:
    paragraph = "The quick brown fox jumps over the lazy dog. " * 20
    return [paragraph.strip()] * 80


def _whitespace_reformatted_content() -> tuple[str, str]:
    """Build a ~41 KB doc and a whitespace-only reformatted version.

    The two versions differ ONLY in whitespace (indent changed from 4-space to 2-space).
    The span heuristic counts this as ~70K chars lost (first diff at pos 2, last diff
    near end). The fix must recognize zero real content changed.
    """
    paragraphs = _paragraphs_for_41k()
    baseline = _make_content(paragraphs, indent="    ")
    new_content = _reindent(baseline, "    ", "  ")
    return baseline, new_content


class TestWhitespaceOnlyReformatNoTrigger:
    """Scattered whitespace reformatting must NOT trigger content-loss backup."""

    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_indent_change_no_trigger(self, mock_get_db):
        """Changing indentation from 4-space to 2-space across a ~41 KB doc must NOT
        trigger a backup — zero real content is lost."""
        baseline, new_content = _whitespace_reformatted_content()
        assert len(baseline) > 30000

        mock_db = AsyncMock()
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc-indent-test", new_content, baseline_content=baseline,
        )
        assert result is None

    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_tabs_to_spaces_no_trigger(self, mock_get_db):
        """Converting tabs to spaces must NOT trigger a backup."""
        paragraphs = _paragraphs_for_41k()
        baseline = _make_content(paragraphs, indent="\t")
        new_content = _reindent(baseline, "\t", "        ")

        assert len(baseline) != len(new_content)
        mock_db = AsyncMock()
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc-tab-test", new_content, baseline_content=baseline,
        )
        assert result is None

    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_trailing_whitespace_change_no_trigger(self, mock_get_db):
        """Adding/removing trailing spaces on lines must NOT trigger a backup."""
        baseline = "Hello world\n" * 100
        new_content = "Hello world   \n" * 100

        mock_db = AsyncMock()
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc-trailing-test", new_content, baseline_content=baseline,
        )
        assert result is None

    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_collapse_blank_lines_no_trigger(self, mock_get_db):
        """Collapsing multiple blank lines to single blanks must NOT trigger."""
        baseline = "Hello\n\n\n\n\nWorld\n\n\n\n\nFoo\n\n\n\n\nBar"
        new_content = "Hello\n\nWorld\n\nFoo\n\nBar"

        mock_db = AsyncMock()
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc-blank-test", new_content, baseline_content=baseline,
        )
        assert result is None

    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_pure_newline_normalize_no_trigger(self, mock_get_db):
        """Normalizing CRLF to LF must NOT trigger."""
        baseline = "Line one\r\nLine two\r\nLine three\r\n" * 50
        new_content = baseline.replace("\r\n", "\n")

        mock_db = AsyncMock()
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc-crlf-test", new_content, baseline_content=baseline,
        )
        assert result is None


class TestGenuineLossStillTriggers:
    """Genuine content deletion/replacement must still trigger."""

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_select_all_replace_equal_length(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """A genuine select-all-replace of equal length (different content) MUST trigger."""
        baseline = "A" * 1000
        new_content = "B" * 1000

        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": baseline}

        result = await maybe_backup_on_content_loss(
            "doc-replace-test", new_content, baseline_content=baseline,
        )
        assert result is not None
        mock_create.assert_called_once()

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_large_prefix_truncation(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """Removing the first half of a document MUST trigger."""
        baseline = "X" * 1000
        new_content = "X" * 400

        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": baseline}

        result = await maybe_backup_on_content_loss(
            "doc-trunc-test", new_content, baseline_content=baseline,
        )
        assert result is not None

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_real_content_swap_in_middle(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """Replacing a large middle section with different text MUST trigger."""
        prefix = "Header line\n" * 10
        middle_old = "Old content paragraph.\n" * 50
        suffix = "Footer line\n" * 10
        baseline = prefix + middle_old + suffix

        middle_new = "Completely different replacement text.\n" * 50
        new_content = prefix + middle_new + suffix

        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": baseline}

        result = await maybe_backup_on_content_loss(
            "doc-middle-test", new_content, baseline_content=baseline,
        )
        assert result is not None


class TestCommentReflectsRealChangedChars:
    """The backup comment must report actual changed characters, not the span."""

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_comment_shows_real_chars_not_span(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """When scattered real edits span a large area but are small in total,
        the comment must report the real changed-char count, not the span.

        Layout: 20 chunks of 50 A's + 30 B's, baseline vs same A's + 30 C's.
        Span heuristic would report ~1550 chars (total minus shared prefix of 50).
        Real changed chars: 20 × 30 = 600 (each B-block replaced by C-block).
        """
        chunk_old = "A" * 50 + "B" * 30
        chunk_new = "A" * 50 + "C" * 30
        baseline = chunk_old * 20
        new_content = chunk_new * 20

        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": baseline}

        result = await maybe_backup_on_content_loss(
            "doc-comment-test", new_content, baseline_content=baseline,
        )
        assert result is not None

        comment = mock_create.call_args.kwargs.get("comment", "")
        assert "600 chars" in comment
        assert "1550" not in comment


class TestDiffCapFallback:
    """When input exceeds the diff cap, fallback to span heuristic still works."""

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_large_input_fallback_still_triggers(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """Even if input is too large for difflib (above cap), genuine loss still triggers
        via the span fallback."""
        baseline = "A" * 500_000
        new_content = "B" * 500_000

        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": baseline}

        result = await maybe_backup_on_content_loss(
            "doc-large-test", new_content, baseline_content=baseline,
        )
        assert result is not None
