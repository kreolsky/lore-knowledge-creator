"""Unit tests for markdown chunker — no DB dependencies.

S2 contract (chunking-and-embedding-quality plan): the chunker now
- splits recursively down to sentence/space boundaries so no chunk can exceed
  the provider's per-string limit (ROOT CAUSE: oversized single chunk),
- assigns the section heading to its FIRST chunk only (no alternating dedup bug),
- carries offsets positionally (not via text.find, which drifts on repeats),
- merges sub-minimum trailing chunks into a neighbour,
- strips transclusion embed nodes (![alt](target)) from chunk text,
- reuses deps.extract_headings (skips headings inside fenced code blocks).
"""


import pytest
from markdown_chunker import chunk_markdown

from config import EMBEDDING_INPUT_MAX_CHARS, RETRIEVAL_CHUNK_MAX_CHARS

_LIMITS = {
    "max_chunk_chars": RETRIEVAL_CHUNK_MAX_CHARS,
    "input_max_chars": EMBEDDING_INPUT_MAX_CHARS,
}


class TestChunkMarkdown:

    def test_whitespace_only(self):
        assert chunk_markdown("   \n  \n  ", **_LIMITS) == []


    # ── S2: positional offsets (D10 rewrite — was unique paragraphs, now repeated) ──


    # ── S2: recursive split — no chunk may exceed the provider limit (ROOT CAUSE) ──


    def test_soft_target_above_ceiling_is_capped_at_ceiling(self):
        # Both limits are live admin knobs, so an operator can set the chunk size
        # above the input ceiling; a paragraph that fits the soft target must still
        # be split to the ceiling, or the provider rejects the chunk (HTTP 400).
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 500 for i in range(4))
        chunks = chunk_markdown(text, max_chunk_chars=3000, input_max_chars=1000)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.content) <= 1000


    # ── S2: heading dedup — first chunk of a section carries the heading only ──


    # ── S2: minimum chunk size — tiny trailing chunk merges into neighbour ──

    def test_minimum_chunk_size_merges_tiny_tail(self):
        # A 1-char chunk is retrieval noise. The tail that would fall below the
        # minimum merges into the previous chunk rather than standing alone.
        body = "x" * 600
        text = f"## A\n{body}\n\nhi"  # a 2-char trailing paragraph
        chunks = chunk_markdown(text, max_chunk_chars=300, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        # The "hi" tail must not survive as its own sub-minimum chunk.
        assert all(len(c.content) >= 50 for c in chunks)

    def test_tiny_tail_merge_respects_hard_ceiling(self):
        # F2: a sub-minimum tail must NOT merge into a previous span already at the
        # hard ceiling — that pushes content past EMBEDDING_INPUT_MAX_CHARS and the
        # provider rejects it (the ROOT CAUSE S2 eliminated). The ceiling is a provider
        # contract; the minimum size is only a quality preference, so the ceiling wins
        # and the tiny tail survives as its own (sub-minimum) chunk.
        text = "A" * 16000 + " tail."  # no separators except the space before 'tail'
        chunks = chunk_markdown(text, **_LIMITS)
        assert chunks
        for c in chunks:
            assert len(c.content) <= EMBEDDING_INPUT_MAX_CHARS, (
                f"chunk ord={c.ord} len={len(c.content)} exceeds hard ceiling "
                f"{EMBEDDING_INPUT_MAX_CHARS}"
            )

    # ── S2: reuse deps.extract_headings — fenced code blocks are not headings ──


    # ── S2: strip transclusion embed nodes from chunk text (D6) ──


class TestEmbedTextsResilient:
    """Per-chunk failure policy (ROOT CAUSE §4): one rejected string must not sink
    the whole document. The resilient helper isolates a failing string via bisection."""


    @pytest.mark.asyncio
    async def test_transient_error_propagates_without_storm(self, monkeypatch):
        # A 429/5xx is provider-wide + transient — bisecting only multiplies failing
        # POSTs into a serialized O(N) storm. It must propagate (fail fast) after a
        # single call so the debounce retries the whole batch later.
        import httpx

        import embeddings

        calls = {"n": 0}

        async def fake_embed(texts):
            calls["n"] += 1
            req = httpx.Request("POST", "http://x/embeddings")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("Too Many Requests", request=req, response=resp)

        monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
        with pytest.raises(httpx.HTTPStatusError):
            await embeddings._embed_texts_resilient(["a", "b", "c", "d"])
        assert calls["n"] == 1  # no bisection storm — exactly one failing call


    @pytest.mark.asyncio
    async def test_empty_input(self):
        import embeddings

        results, failed = await embeddings._embed_texts_resilient([])
        assert results == [] and failed == []


class TestAncestorStack:
    """S1 (D1/D5): each chunk carries the ancestor heading path so the embedded text
    can be prefixed with 'Title > H1 > H2'. The path is the level-stack at the chunk's
    section — siblings do not inherit each other's parents."""


    def test_preamble_chunk_has_empty_path(self):
        text = "Intro with no heading.\n\n## First\nbody."
        chunks = chunk_markdown(text, **_LIMITS)
        preamble = [c for c in chunks if c.heading is None]
        assert preamble and preamble[0].heading_path == ()


class TestBreadcrumbText:
    """S1 (D1): the embedded string is 'Title > H1 > H2\\n\\n{content}'; the stored
    content passed to callers stays bare. Built by _breadcrumb_text(title, chunk)."""


    def test_no_title_no_path(self):
        from markdown_chunker import Chunk, _breadcrumb_text

        c = Chunk(ord=0, heading=None, content="bare body.", offset_start=0, offset_end=10)
        assert _breadcrumb_text("", c) == "bare body."


class TestEmbedDocumentTaskTransientRetry:
    """F5: a transient provider error (429 / 5xx) re-enqueues via arq.Retry instead of
    terminating as embedding_status='failed'. A rate limit dropping a document out of the
    index indefinitely is the failure S2 eliminated, only with a different trigger."""


    @pytest.mark.asyncio
    async def test_terminal_error_still_marks_failed(self, monkeypatch):
        # F5 contract guard: a NON-transient failure (RuntimeError from _reembed) must
        # STILL terminate as today — embedding_status='failed' + degraded counter. The
        # transient Retry is only for 429/5xx; everything else stays terminal.
        import db as dbmod
        import embeddings
        import jobs.tasks as tasks

        class _RecordingDB:
            def __init__(self):
                self.queries = []

            async def query(self, q, params=None):
                self.queries.append(q)
                return []

        fake_db = _RecordingDB()

        async def fake_get_db():
            return fake_db

        monkeypatch.setattr(dbmod, "get_db", fake_get_db)

        async def boom(*a, **kw):
            raise RuntimeError("embedding rejected all changed chunks")

        monkeypatch.setattr(embeddings, "_reembed", boom)

        failure = []

        async def record_failure(*a, **kw):
            failure.append(a)

        monkeypatch.setattr(embeddings, "_on_embed_failure", record_failure)

        ctx = {"job_id": "embed:t2", "job_try": 0, "redis": None}
        with pytest.raises(RuntimeError):
            await tasks.embed_document_task(ctx, "doc", "d2", "p1")

        # Terminal: 'failed' status persisted + degraded counter incremented.
        assert any("failed" in q for q in fake_db.queries), fake_db.queries
        assert failure
