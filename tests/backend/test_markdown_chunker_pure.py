"""DB-free tests for the pure markdown chunker (markdown_chunker).

The chunker was extracted from embeddings.py precisely so it is testable WITHOUT a database, an HTTP client, or the event bus.
The import-purity test pins that seam — if markdown_chunker ever grows an import
of embeddings/db/httpx, it stops being the pure algorithm module this file
asserts it is.
"""
import subprocess
import sys

import pytest


def test_importing_the_chunker_pulls_no_io_modules():
    """The seam itself: importing markdown_chunker must not import embeddings,
    db, or httpx (a fresh interpreter, so no conftest imports can mask it)."""
    code = (
        "import sys; import markdown_chunker; "
        "bad = [m for m in ('embeddings', 'db', 'httpx', 'event_bus') if m in sys.modules]; "
        "assert not bad, f'chunker dragged in I/O modules: {bad}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd="/app",
    )
    assert proc.returncode == 0, proc.stderr


def test_small_doc_chunks_with_valid_offsets():
    """A two-section doc chunks per section; every chunk's content is the
    projection of its own offset span (the pinned contract of _emit_chunks)."""
    from markdown_chunker import chunk_markdown

    text = "# A\n\nfirst section body.\n\n# B\n\nsecond section body."
    chunks = chunk_markdown(text)
    assert [c.heading for c in chunks] == ["# A", "# B"]
    assert [c.heading_path for c in chunks] == [("A",), ("B",)]
    for c in chunks:
        span = text[c.offset_start:c.offset_end]
        assert span.strip() == c.content, (span, c.content)


def test_no_separator_overflow_never_exceeds_hard_ceiling():
    """A base64-like blob with no whitespace must hard char-split at the provider
    ceiling — the INVARIANT the recursive splitter exists for."""
    from markdown_chunker import chunk_markdown

    from config import EMBEDDING_INPUT_MAX_CHARS

    blob = "A" * (EMBEDDING_INPUT_MAX_CHARS * 2 + 17)
    # `max_chunk_chars` cannot be honoured with no separator to split on — the
    # provider ceiling is the bound that must still hold.
    chunks = chunk_markdown(blob, max_chunk_chars=200)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.content) <= EMBEDDING_INPUT_MAX_CHARS
    # No content lost: the chunks tile the blob, by content AND by offset span.
    assert "".join(c.content for c in chunks) == blob
    assert chunks[0].offset_start == 0
    assert chunks[-1].offset_end == len(blob)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.offset_end == nxt.offset_start


def test_chunk_hash_includes_the_breadcrumb():
    """Two chunks with the same content but a different heading path hash
    differently — renaming a section invalidates its vectors."""
    from markdown_chunker import Chunk, _chunk_hash

    import config

    body = dict(ord=0, heading=None, content="body", offset_start=0, offset_end=4)
    under_h2 = Chunk(heading_path=("Top", "Mid"), **body)
    under_h3 = Chunk(heading_path=("Top", "Mid", "Deep"), **body)
    assert _chunk_hash("Doc", under_h2, config.EMBEDDING_MODEL) != \
        _chunk_hash("Doc", under_h3, config.EMBEDDING_MODEL)


def test_chunk_hash_model_argument_is_required():
    """`model` is a REQUIRED argument (plan: instance-settings-debt step 2) — the
    hash never reads config, so there is no default leg. A call without it must
    fail loudly, not silently fall back to an env-folded model name."""
    from markdown_chunker import Chunk, _chunk_hash

    c = Chunk(ord=0, heading=None, content="body", offset_start=0, offset_end=4)
    with pytest.raises(TypeError):
        _chunk_hash("T", c)
