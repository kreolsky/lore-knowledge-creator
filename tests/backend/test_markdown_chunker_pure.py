"""DB-free tests for the pure markdown chunker (markdown_chunker).

The chunker was extracted from embeddings.py precisely so it is testable WITHOUT a database, an HTTP client, or the event bus.
The import-purity test pins that seam — if markdown_chunker ever grows an import
of embeddings/db/httpx, it stops being the pure algorithm module this file
asserts it is.
"""


def test_small_doc_chunks_with_valid_offsets():
    """A two-section doc chunks per section; every chunk's content is the
    projection of its own offset span (the pinned contract of _emit_chunks)."""
    from markdown_chunker import chunk_markdown

    text = "# A\n\nfirst section body.\n\n# B\n\nsecond section body."
    chunks = chunk_markdown(text, max_chunk_chars=3000, input_max_chars=8000)
    assert [c.heading for c in chunks] == ["# A", "# B"]
    assert [c.heading_path for c in chunks] == [("A",), ("B",)]
    for c in chunks:
        span = text[c.offset_start:c.offset_end]
        assert span.strip() == c.content, (span, c.content)
