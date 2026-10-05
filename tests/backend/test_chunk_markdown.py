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

from unittest.mock import AsyncMock, patch

import pytest
from markdown_chunker import chunk_markdown

from config import EMBEDDING_INPUT_MAX_CHARS, RETRIEVAL_CHUNK_MAX_CHARS

_LIMITS = {
    "max_chunk_chars": RETRIEVAL_CHUNK_MAX_CHARS,
    "input_max_chars": EMBEDDING_INPUT_MAX_CHARS,
}


class TestChunkMarkdown:
    def test_empty_text(self):
        assert chunk_markdown("", **_LIMITS) == []

    def test_whitespace_only(self):
        assert chunk_markdown("   \n  \n  ", **_LIMITS) == []

    def test_plain_text_no_headings(self):
        text = "Hello world. This is a plain paragraph."
        chunks = chunk_markdown(text, **_LIMITS)
        assert len(chunks) == 1
        assert chunks[0].heading is None
        assert chunks[0].content == text
        assert chunks[0].offset_start == 0
        assert chunks[0].offset_end == len(text)

    def test_h2_sections(self):
        text = "## Section One\nContent one.\n\n## Section Two\nContent two."
        chunks = chunk_markdown(text, **_LIMITS)
        assert len(chunks) == 2
        assert chunks[0].heading == "## Section One"
        assert "Content one" in chunks[0].content
        assert chunks[1].heading == "## Section Two"
        assert "Content two" in chunks[1].content

    def test_h3_nested(self):
        text = "## Parent\nParent content.\n### Child\nChild content."
        chunks = chunk_markdown(text, **_LIMITS)
        assert len(chunks) == 2
        assert chunks[0].heading == "## Parent"
        assert chunks[1].heading == "### Child"

    def test_long_section_split_by_paragraphs(self):
        max_chars = 200
        para = "Word " * 40
        text = f"## Long\n{para}\n\n{para}\n\n{para}"
        chunks = chunk_markdown(text, max_chunk_chars=max_chars, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        assert len(chunks) > 1
        # S2: every chunk stays under the hard provider ceiling (soft target is a hint).
        for c in chunks:
            assert len(c.content) <= EMBEDDING_INPUT_MAX_CHARS

    def test_single_h1(self):
        text = "# Title\nBody text here."
        chunks = chunk_markdown(text, **_LIMITS)
        assert len(chunks) == 1
        assert chunks[0].heading == "# Title"

    def test_mixed_heading_levels(self):
        text = "# H1\nh1 body\n## H2\nh2 body\n### H3\nh3 body"
        chunks = chunk_markdown(text, **_LIMITS)
        assert len(chunks) == 3
        assert chunks[0].heading == "# H1"
        assert chunks[1].heading == "## H2"
        assert chunks[2].heading == "### H3"

    # ── S2: positional offsets (D10 rewrite — was unique paragraphs, now repeated) ──

    def test_offset_accuracy(self):
        # The old chunker located each chunk via text.find(content) (FIRST match).
        # The drift only compounds past the FIRST flush (chunk_start is reused), so
        # this needs three identical paragraphs → three chunks → two flushes. Each
        # chunk must point at its OWN copy and START on real content, not the "\n\n"
        # gap (the old .find landed chunk 2/3 inside the gap before the copy).
        para = "Sentence one here. " * 13  # ~247 chars, one paragraph, no newline
        text = f"## A\n{para}\n\n{para}\n\n{para}"
        chunks = chunk_markdown(text, max_chunk_chars=300, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        assert len(chunks) == 3
        starts = [c.offset_start for c in chunks]
        assert starts == sorted(starts)  # strictly increasing
        for c in chunks:
            assert text[c.offset_start:c.offset_end].strip() == c.content.strip()
        # No chunk may start on the whitespace gap between repeated copies.
        for c in chunks:
            assert text[c.offset_start] != "\n"

    # ── S2: recursive split — no chunk may exceed the provider limit (ROOT CAUSE) ──

    def test_oversized_single_line_must_split(self):
        # ROOT CAUSE: a transcript arrives as one line with no newline. The old
        # chunker (split on \n\n only) produced a single oversized chunk and the
        # provider rejected it, so the whole document stayed out of the index.
        text = "word " * 4000  # 20k chars, single line, no newline
        chunks = chunk_markdown(text, max_chunk_chars=3000, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.content) <= EMBEDDING_INPUT_MAX_CHARS

    def test_chunk_never_exceeds_ceiling_with_wide_separators(self):
        # Regression (review CRITICAL): the packer bounded the leaf-text SUM, not the
        # span. Many one-char paragraphs packed into one chunk whose content — which
        # includes the \n\n separators BETWEEN the leaves — exceeded the hard ceiling.
        # The packer now bounds by span width (leaves[j].end - leaves[i].start).
        text = "## A\n" + ("x\n\n" * 3000)  # 3000 one-char paragraphs, wide separators
        chunks = chunk_markdown(text, max_chunk_chars=3000, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        assert len(chunks) > 1  # must split — must NOT collapse to one 8998-char chunk
        for c in chunks:
            assert len(c.content) <= EMBEDDING_INPUT_MAX_CHARS

    def test_soft_target_above_ceiling_is_capped_at_ceiling(self):
        # Both limits are live admin knobs, so an operator can set the chunk size
        # above the input ceiling; a paragraph that fits the soft target must still
        # be split to the ceiling, or the provider rejects the chunk (HTTP 400).
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 500 for i in range(4))
        chunks = chunk_markdown(text, max_chunk_chars=3000, input_max_chars=1000)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.content) <= 1000

    def test_sentence_level_split(self):
        # A single paragraph longer than the soft target splits at sentence
        # boundaries (descends past \n\n / \n to [.!?] + space).
        sent = "This is a clear sentence. "  # 26 chars
        text = "## A\n" + sent * 100  # ~2600 chars, no newline
        chunks = chunk_markdown(text, max_chunk_chars=300, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.content) <= EMBEDDING_INPUT_MAX_CHARS
        # No content is lost across the split (sentences may repeat at overlap seams).
        joined = " ".join(c.content for c in chunks)
        assert joined.count("clear sentence") >= 100

    # ── S2: heading dedup — first chunk of a section carries the heading only ──

    def test_heading_only_on_first_chunk_of_section(self):
        # The old dedup compared against result[-1].heading, so once a chunk was
        # set to None the next compared None != heading and got it back — the
        # label alternated on/off down a long section. It must appear ONCE.
        para = "Body content line. " * 30  # ~600 chars → forces a split under 300
        text = f"## Only\n{para}"
        chunks = chunk_markdown(text, max_chunk_chars=300, input_max_chars=EMBEDDING_INPUT_MAX_CHARS)
        assert len(chunks) >= 3
        assert chunks[0].heading == "## Only"
        # Every subsequent chunk in THIS section inherits None (no alternation).
        for c in chunks[1:]:
            assert c.heading is None

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

    def test_heading_inside_fenced_code_is_ignored(self):
        # The bare _HEADING_RE split on "# comment" inside a code fence. The shared
        # parser skips fenced blocks, so a fence does not open a section here.
        text = (
            "## Real\nIntro.\n\n"
            "```python\n# not a heading\nx = 1\n```\n\n"
            "After code."
        )
        chunks = chunk_markdown(text, **_LIMITS)
        headings = [c.heading for c in chunks if c.heading]
        assert headings == ["## Real"]
        # The fenced "# not a heading" must not become chunk content's heading.
        assert all("not a heading" not in (c.heading or "") for c in chunks)

    def test_h5_opens_section(self):
        # The chunker requests H1–H6 from extract_headings (max_level=6), so an H5
        # opens its own section. Guards that the depth is not silently capped at H4.
        text = "## H2\nbody\n##### H5\nh5 body"
        chunks = chunk_markdown(text, **_LIMITS)
        assert any(c.heading == "##### H5" for c in chunks)

    # ── S2: strip transclusion embed nodes from chunk text (D6) ──

    def test_transclusion_node_is_stripped(self):
        # A transclusion embed (![alt](doc:<id>)) is a pointer, not prose — embedding
        # it as a URL string is noise. It is dropped from chunk content. A real
        # external image is left intact.
        text = (
            "## A\n"
            "See the embedded part ![section](doc:abc-123) inline.\n\n"
            "And a real image ![pic](https://example.com/x.png) here."
        )
        chunks = chunk_markdown(text, **_LIMITS)
        joined = "\n".join(c.content for c in chunks)
        assert "doc:abc-123" not in joined
        assert "![section](doc:abc-123)" not in joined
        # External image is not a transclusion — preserved.
        assert "https://example.com/x.png" in joined

    def test_transclusion_offsets_project_to_content(self):
        # F3 (D10): offsets define the SOURCE SPAN the chunk was derived from; content
        # is its PROJECTION. Stripping a transclusion node (or padding whitespace) makes
        # offset_end - offset_start != len(content), so the contract is the projection:
        # _strip_transclusions(text[start:end]).strip() == content. The old bounds-only
        # check (0 <= s <= e <= len) had no failing branch and pinned nothing.
        from markdown_chunker import _strip_transclusions

        text = "## A\nBefore ![t](ref:r1) after."
        chunks = chunk_markdown(text, **_LIMITS)
        assert chunks
        for c in chunks:
            assert 0 <= c.offset_start <= c.offset_end <= len(text)
            projected = _strip_transclusions(text[c.offset_start:c.offset_end]).strip()
            assert projected == c.content

    def test_offsets_tightened_past_leading_whitespace(self):
        # F3: offset_start must land on the first non-whitespace char of content (the
        # scroll target Sources.tsx:31 seeks), not the raw span start which may sit on
        # padding whitespace. Tightening to the post-.strip() bounds is the computable
        # part; interior transclusion nodes remain in the span as the accepted residual.
        text = "## A\n\n   indented body."
        chunks = chunk_markdown(text, **_LIMITS)
        body = [c for c in chunks if "indented" in c.content]
        assert body, "expected a chunk carrying the indented body"
        c = body[0]
        assert text[c.offset_start] == "i", (
            f"offset_start={c.offset_start} lands on {text[c.offset_start]!r}, "
            f"expected 'i' (first content char)"
        )


class TestEmbedTextsResilient:
    """Per-chunk failure policy (ROOT CAUSE §4): one rejected string must not sink
    the whole document. The resilient helper isolates a failing string via bisection."""

    @pytest.mark.asyncio
    async def test_isolates_single_failure(self, monkeypatch):
        import httpx

        import embeddings

        async def fake_embed(texts):
            if any("POISON" in t for t in texts):
                req = httpx.Request("POST", "http://x/embeddings")
                resp = httpx.Response(400, request=req)
                raise httpx.HTTPStatusError("Bad Request", request=req, response=resp)
            return [[0.1] * 4 for _ in texts]

        monkeypatch.setattr(embeddings, "embed_texts", fake_embed)

        texts = ["good alpha", "POISON beta", "good gamma"]
        results, failed = await embeddings._embed_texts_resilient(texts)

        assert failed == [1]
        assert len(results) == 3
        assert results[0] is not None and results[1] is None and results[2] is not None

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
    async def test_all_good_returns_full_batch(self, monkeypatch):
        import embeddings

        async def fake_embed(ts):
            return [[0.2] * 4 for _ in ts]

        monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
        results, failed = await embeddings._embed_texts_resilient(["a", "b", "c"])
        assert failed == []
        assert all(r is not None for r in results)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_empty_input(self):
        import embeddings

        results, failed = await embeddings._embed_texts_resilient([])
        assert results == [] and failed == []


class TestAncestorStack:
    """S1 (D1/D5): each chunk carries the ancestor heading path so the embedded text
    can be prefixed with 'Title > H1 > H2'. The path is the level-stack at the chunk's
    section — siblings do not inherit each other's parents."""

    def test_heading_path_nested(self):
        text = "# Top\ntop body.\n## Mid\nmid body.\n### Deep\ndeep body."
        chunks = chunk_markdown(text, **_LIMITS)
        by_heading = {c.heading: c for c in chunks if c.heading}
        assert by_heading["# Top"].heading_path == ("Top",)
        assert by_heading["## Mid"].heading_path == ("Top", "Mid")
        assert by_heading["### Deep"].heading_path == ("Top", "Mid", "Deep")

    def test_heading_path_resets_on_sibling(self):
        # An H1 after an H2 must NOT carry the earlier H2 — the stack pops on level.
        text = "# A\na body.\n## B\nb body.\n# C\nc body."
        chunks = chunk_markdown(text, **_LIMITS)
        by_heading = {c.heading: c for c in chunks if c.heading}
        assert by_heading["# A"].heading_path == ("A",)
        assert by_heading["## B"].heading_path == ("A", "B")
        assert by_heading["# C"].heading_path == ("C",)

    def test_preamble_chunk_has_empty_path(self):
        text = "Intro with no heading.\n\n## First\nbody."
        chunks = chunk_markdown(text, **_LIMITS)
        preamble = [c for c in chunks if c.heading is None]
        assert preamble and preamble[0].heading_path == ()


class TestBreadcrumbText:
    """S1 (D1): the embedded string is 'Title > H1 > H2\\n\\n{content}'; the stored
    content passed to callers stays bare. Built by _breadcrumb_text(title, chunk)."""

    def test_full_path(self):
        from markdown_chunker import Chunk, _breadcrumb_text

        c = Chunk(ord=0, heading="### Deep", content="deep body.",
                  offset_start=0, offset_end=9, heading_path=("Top", "Mid", "Deep"))
        assert _breadcrumb_text("My Doc", c) == "My Doc > Top > Mid > Deep\n\ndeep body."

    def test_no_title_no_path(self):
        from markdown_chunker import Chunk, _breadcrumb_text

        c = Chunk(ord=0, heading=None, content="bare body.", offset_start=0, offset_end=10)
        assert _breadcrumb_text("", c) == "bare body."

    def test_title_only(self):
        from markdown_chunker import Chunk, _breadcrumb_text

        c = Chunk(ord=0, heading=None, content="body.", offset_start=0, offset_end=5)
        assert _breadcrumb_text("Doc Title", c) == "Doc Title\n\nbody."


class TestEmbedTextsInstruction:
    """S1 (D3): the Qwen3 query instruction is an OPT-IN param on embed_texts —
    documents are embedded plain, queries are prefixed. Never a default."""

    @pytest.mark.asyncio
    async def test_instruction_prefixes_each_input(self, http_pool):
        from unittest.mock import MagicMock

        import embeddings

        captured: dict = {}

        async def fake_post(url, *, headers=None, json=None, **kw):
            captured["input"] = json["input"]
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "data": [{"index": i, "embedding": [0.0] * 4} for i in range(len(json["input"]))]
            }
            return resp

        mock_client = MagicMock()
        mock_client.post = fake_post
        http_pool("embeddings", mock_client)
        with patch.object(embeddings, "_ensure_config", new_callable=AsyncMock):
            await embeddings.embed_texts(
                ["what is the protocol?", "second query"],
                instruction="Given a user query, retrieve relevant document passages",
            )
        assert captured["input"] == [
            "Instruct: Given a user query, retrieve relevant document passages\nQuery: what is the protocol?",
            "Instruct: Given a user query, retrieve relevant document passages\nQuery: second query",
        ]

    @pytest.mark.asyncio
    async def test_no_instruction_is_passthrough(self, http_pool):
        from unittest.mock import MagicMock

        import embeddings

        captured: dict = {}

        async def fake_post(url, *, headers=None, json=None, **kw):
            captured["input"] = json["input"]
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "data": [{"index": 0, "embedding": [0.0] * 4}]
            }
            return resp

        mock_client = MagicMock()
        mock_client.post = fake_post
        http_pool("embeddings", mock_client)
        with patch.object(embeddings, "_ensure_config", new_callable=AsyncMock):
            await embeddings.embed_texts(["plain document text"])
        assert captured["input"] == ["plain document text"]


class TestIncrementalReembed:
    """S3 (D7/D11): a content_hash over the EMBEDDED text (breadcrumb included) lets a
    re-embed SKIP the embedding API call for chunks whose text did not change. These test
    the pure planning helpers; the live reuse is demonstrated by re-embedding twice."""

    def test_chunk_hash_includes_breadcrumb(self):
        from markdown_chunker import Chunk, _chunk_hash

        import config

        same_content = "the body text."
        under_h2 = Chunk(ord=0, heading="## B", content=same_content,
                         offset_start=0, offset_end=14, heading_path=("A", "B"))
        under_h3 = Chunk(ord=0, heading="### C", content=same_content,
                         offset_start=0, offset_end=14, heading_path=("A", "B", "C"))
        # Same content, different heading path -> different embedded text -> different hash.
        assert _chunk_hash("Doc", under_h2, config.EMBEDDING_MODEL) != \
            _chunk_hash("Doc", under_h3, config.EMBEDDING_MODEL)
        # Same content + same path -> same hash (stable across a no-op re-embed).
        assert _chunk_hash("Doc", under_h2, config.EMBEDDING_MODEL) == \
            _chunk_hash("Doc", under_h2, config.EMBEDDING_MODEL)

    def test_chunk_hash_changes_on_title_rename(self):
        from markdown_chunker import Chunk, _chunk_hash

        import config

        c = Chunk(ord=0, heading="## S", content="body.", offset_start=0, offset_end=5,
                  heading_path=("S",))
        # Renaming the document title invalidates every chunk under it (D7 risk note).
        assert _chunk_hash("Old Title", c, config.EMBEDDING_MODEL) != \
            _chunk_hash("New Title", c, config.EMBEDDING_MODEL)

    def test_chunk_hash_changes_with_embedding_model(self, monkeypatch):
        # F4: the hash must mix in EMBEDDING_MODEL. Without it, swapping the model
        # reuses every stored vector forever and the table silently holds two models'
        # geometry in one column. Same chunk + different model name -> different hash.
        from markdown_chunker import Chunk, _chunk_hash

        import config

        c = Chunk(ord=0, heading="## S", content="body.", offset_start=0, offset_end=5,
                  heading_path=("S",))
        h_before = _chunk_hash("Doc", c, config.EMBEDDING_MODEL)
        monkeypatch.setattr(config, "EMBEDDING_MODEL", "other/model-7b")
        h_after = _chunk_hash("Doc", c, config.EMBEDDING_MODEL)
        assert h_before != h_after

    def test_plan_reuse_matches_by_hash(self):
        from embeddings import _plan_reuse

        existing = [
            {"id": "row-a", "content_hash": "h1", "ord": 0},
            {"id": "row-b", "content_hash": "h2", "ord": 1},
        ]
        # New chunks: [h1 (unchanged), h3 (changed)]
        reuse, to_embed = _plan_reuse(["h1", "h3"], existing)
        assert reuse == {0: "row-a"}        # h1 reuses row-a
        assert to_embed == [1]              # h3 needs embedding

    def test_plan_reuse_no_cross_match_on_duplicates(self):
        from embeddings import _plan_reuse

        # Two identical new chunks (same hash) but only one existing row with that hash:
        # one is reused, the other must be embedded (no cross-matching).
        existing = [{"id": "row-x", "content_hash": "dup"}]
        reuse, to_embed = _plan_reuse(["dup", "dup"], existing)
        assert reuse == {0: "row-x"}
        assert to_embed == [1]

    def test_plan_reuse_missing_hash_is_changed(self):
        from embeddings import _plan_reuse

        # D11: a row without content_hash (pre-S3) is never reused — the first re-embed
        # after deploy re-embeds it (self-heal), then stores the hash for next time.
        existing = [{"id": "old", "content_hash": None}]
        reuse, to_embed = _plan_reuse(["anything"], existing)
        assert reuse == {}
        assert to_embed == [0]

    def test_unchanged_detects_noop_reembed(self):
        from markdown_chunker import Chunk

        from embeddings import _unchanged

        chunks = [Chunk(ord=0, heading="## A", content="x", offset_start=3, offset_end=4,
                        heading_path=("A",))]
        hashes = ["h0"]
        existing = [{"content_hash": "h0", "ord": 0, "offset_start": 3, "offset_end": 4,
                     "model": "m1"}]
        assert _unchanged(chunks, hashes, existing, "m1") is True
        # A shifted offset is NOT a no-op — the chunk still needs an UPDATE.
        existing_shifted = [{"content_hash": "h0", "ord": 0, "offset_start": 9, "offset_end": 10,
                             "model": "m1"}]
        assert _unchanged(chunks, hashes, existing_shifted, "m1") is False


class TestEmbedDocumentTaskTransientRetry:
    """F5: a transient provider error (429 / 5xx) re-enqueues via arq.Retry instead of
    terminating as embedding_status='failed'. A rate limit dropping a document out of the
    index indefinitely is the failure S2 eliminated, only with a different trigger."""

    @pytest.mark.asyncio
    async def test_transient_429_raises_retry_not_failed(self, monkeypatch):
        import httpx
        from arq.worker import Retry

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

        req = httpx.Request("POST", "http://x/embeddings")
        resp = httpx.Response(429, request=req)

        async def boom(*a, **kw):
            raise httpx.HTTPStatusError("Too Many Requests", request=req, response=resp)

        monkeypatch.setattr(embeddings, "_reembed", boom)

        failure_calls = []

        async def record_failure(*a, **kw):
            failure_calls.append(a)

        monkeypatch.setattr(embeddings, "_on_embed_failure", record_failure)

        ctx = {"job_id": "embed:t1", "job_try": 0, "redis": None}
        # The transient path raises Retry (deferred backoff); it must NOT fall through to
        # the terminal handler.
        with pytest.raises(Retry):
            await tasks.embed_document_task(ctx, "doc", "d1", "p1")

        # Terminal side-effects must NOT have fired: no 'failed' status, no degraded count.
        assert not any("failed" in q for q in fake_db.queries), fake_db.queries
        assert failure_calls == []

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
