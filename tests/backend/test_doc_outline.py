"""Plan document-outline-in-structure-map: build_outline + refresh_outline +
the embed-job write path.

The outline is a DERIVED document field: raw headings (H1–H4 cap, the TOC cap
in deps.extract_headings), cleaned (title H1, manual numbering, consecutive
duplicates), rendered flat with " · ", degraded by the level ladder (drop H4,
then H3, then tail) — every dropped heading COUNTED into outline_hidden (the
walk's counted contract). Written from the embed job ABOVE _reembed so a
project without embeddings still gets outlines.
"""

import pytest

# ─── build_outline: pure unit table ──────────────────────────────────────────


def _outline(title: str, content: str, *, max_chars: int | None = None):
    import doc_outline

    if max_chars is not None:
        old = doc_outline.OUTLINE_MAX_CHARS
        doc_outline.OUTLINE_MAX_CHARS = max_chars
        try:
            return doc_outline.build_outline(title, content)
        finally:
            doc_outline.OUTLINE_MAX_CHARS = old
    return doc_outline.build_outline(title, content)


def _headings(content: str) -> list[dict]:
    from deps import extract_headings

    return extract_headings(content)


def test_no_headings_returns_empty():
    assert _outline("T", "Просто текст\nбез заголовков") == ("", 0)
    assert _outline("T", "") == ("", 0)


def test_h1_equal_to_title_is_dropped():
    content = "# Анамнез\n## Осмотр\n## Заключение"
    outline, hidden = _outline("Анамнез", content)
    assert outline == "Осмотр · Заключение"
    assert hidden == 0


def test_manual_numbering_stripped():
    content = "# Анамнез\n## 1.2.3 Жалобы\n## Глава 4. Заключение"
    outline, hidden = _outline("T", content)
    assert outline == "Анамнез · Жалобы · Заключение"
    assert hidden == 0


def test_consecutive_duplicate_headings_collapsed():
    content = "# T\n## А\n## А\n## Б\n## А"
    outline, hidden = _outline("T", content)
    assert outline == "А · Б · А"  # H1==title dropped; only CONSECUTIVE dupes collapse
    assert hidden == 0


def test_fenced_pseudo_headings_excluded():
    content = "# Вступление\n```\n# not a heading\n```\n## Real"
    outline, hidden = _outline("T", content)
    assert outline == "Вступление · Real"
    assert hidden == 0


def test_over_budget_drops_h4_first_h1_h2_h3_survive():
    content = (
        "# Intro\n## Methods\n### Alpha\n### Beta\n"
        "#### A1\n#### A2\n#### A3\n#### A4"
    )
    # Derived counter: the number of H4 headings the input carries — never a
    # literal (.claude/rules/testing.md).
    n_h4 = sum(1 for h in _headings(content) if h["level"] == 4)
    # Full render is 50 chars; at 30 the H4 drop alone must fit.
    assert len("Intro · Methods · Alpha · Beta · A1 · A2 · A3 · A4") == 50
    assert len("Intro · Methods · Alpha · Beta") == 30
    outline, hidden = _outline("T", content, max_chars=30)
    assert outline == "Intro · Methods · Alpha · Beta"
    assert hidden == n_h4 == 4


def test_ladder_descends_to_h3_when_h4_drop_is_not_enough():
    content = (
        "# Intro\n## Methods\n### Alpha\n### Beta\n"
        "#### A1\n#### A2\n#### A3\n#### A4"
    )
    n_h4 = sum(1 for h in _headings(content) if h["level"] == 4)
    n_h3 = sum(1 for h in _headings(content) if h["level"] == 3)
    # "Intro · Methods" is exactly 15 — the inclusive fit boundary.
    assert len("Intro · Methods") == 15
    outline, hidden = _outline("T", content, max_chars=15)
    assert outline == "Intro · Methods"
    assert hidden == n_h4 + n_h3 == 6


def test_tail_drop_touches_h1_h2_only_as_last_resort():
    content = (
        "# Intro\n## Methods\n### Alpha\n### Beta\n"
        "#### A1\n#### A2\n#### A3\n#### A4"
    )
    total = len(_headings(content))
    # At 8 even "Intro · Methods" (15) is over: H4 → H3 → tail. H1 survives.
    outline, hidden = _outline("T", content, max_chars=8)
    assert outline == "Intro"
    assert hidden == total - 1 == 7


def test_single_overlong_heading_drops_to_empty_and_counts():
    long_h1 = "# " + "Ж" * 40
    outline, hidden = _outline("T", long_h1, max_chars=10)
    assert outline == ""
    assert hidden == 1


def test_empty_outline_when_nothing_survives_cleaning():
    # The only heading equals the title — nothing rendered, nothing dropped.
    assert _outline("T", "# T") == ("", 0)


# ─── refresh_outline: the DB write path ──────────────────────────────────────


async def _seed_doc(test_db, doc_id: str, title: str, content: str) -> None:
    from db import create_record

    await test_db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    await create_record("documents", doc_id, {
        "project_id": "p-outline",
        "title": title,
        "content": content,
        "path": f"_outline/{doc_id}.md",
    })


@pytest.mark.asyncio
async def test_refresh_outline_writes_both_fields(test_db):
    from doc_outline import build_outline, refresh_outline

    from db import get_db

    db = await get_db()
    content = "# Анамнез\n## Осмотр\n### Жалобы"
    await _seed_doc(test_db, "outline-doc-1", "Анамнез", content)
    await refresh_outline("outline-doc-1")

    rows = await db.query(
        "SELECT meta::id(id) AS id, outline, outline_hidden FROM documents "
        "WHERE meta::id(id) = 'outline-doc-1'",
    )
    want_outline, want_hidden = build_outline("Анамнез", content)
    assert (rows[0]["outline"], rows[0]["outline_hidden"]) == (
        want_outline, want_hidden or None,  # stored NONE-normalized: 0 → NONE
    )


@pytest.mark.asyncio
async def test_refresh_outline_empty_content_stores_none(test_db):
    from doc_outline import refresh_outline

    from db import get_db

    db = await get_db()
    await _seed_doc(test_db, "outline-doc-2", "Пустой", "")
    await refresh_outline("outline-doc-2")

    rows = await db.query(
        "SELECT outline, outline_hidden FROM documents "
        "WHERE meta::id(id) = 'outline-doc-2'",
    )
    assert rows[0]["outline"] is None
    assert rows[0]["outline_hidden"] is None


@pytest.mark.asyncio
async def test_refresh_outline_overwrites_stale_value(test_db):
    from doc_outline import refresh_outline

    from db import get_db

    db = await get_db()
    await _seed_doc(test_db, "outline-doc-3", "T", "## Свежий")
    await test_db.query(
        "UPDATE type::record('documents', $id) SET outline = $o",
        {"id": "outline-doc-3", "o": "STALE"},
    )
    await refresh_outline("outline-doc-3")

    rows = await db.query(
        "SELECT outline FROM documents WHERE meta::id(id) = 'outline-doc-3'",
    )
    assert rows[0]["outline"] == "Свежий"


@pytest.mark.asyncio
async def test_refresh_outline_missing_doc_is_noop(test_db):
    from doc_outline import refresh_outline

    await refresh_outline("outline-ghost")  # must not raise


@pytest.mark.asyncio
async def test_refresh_outline_skips_write_when_unchanged(test_db, monkeypatch):
    """R2 (phase-4 review): the embed job calls refresh_outline on every debounce
    cycle; a heading-identical body — most edits touch text, not headings — must
    not issue an UPDATE at all. A changed body still lands."""
    import doc_outline

    from db import get_db

    db = await get_db()
    content = "# Анамнез\n## Осмотр"
    await _seed_doc(test_db, "outline-doc-5", "Анамнез", content)
    await doc_outline.refresh_outline("outline-doc-5")  # the first write lands

    updates: list[str] = []

    class _RecordingDB:
        def __init__(self, real):
            self._real = real

        async def query(self, sql, params=None):
            if sql.lstrip().upper().startswith("UPDATE"):
                updates.append(sql)
            return await self._real.query(sql, params)

    async def _spy_db():
        return _RecordingDB(db)

    monkeypatch.setattr("db.get_db", _spy_db)

    await doc_outline.refresh_outline("outline-doc-5")
    assert updates == [], "unchanged outline must not issue an UPDATE"

    await test_db.query(
        "UPDATE type::record('documents', $id) SET content = $c",
        {"id": "outline-doc-5", "c": content + "\n## Жалобы"},
    )
    await doc_outline.refresh_outline("outline-doc-5")
    assert len(updates) == 1, "changed headings must land"


# ─── Embed-job write path: outline refresh ABOVE _reembed ────────────────────


async def _noop_success(project_id):
    return None


@pytest.mark.asyncio
async def test_embed_task_refreshes_outline_before_reembed(monkeypatch):
    from jobs.tasks.embed import embed_document_task

    order: list[str] = []

    async def _record_refresh(entity_id):
        order.append("refresh")

    async def _record_reembed(*a, **k):
        order.append("reembed")

    monkeypatch.setattr("doc_outline.refresh_outline", _record_refresh)
    monkeypatch.setattr("embeddings._reembed", _record_reembed)
    monkeypatch.setattr("embeddings._on_embed_success", _noop_success)
    await embed_document_task({"job_try": 1}, "doc", "emb-doc-1", "p1")

    assert order == ["refresh", "reembed"], "outline must land ABOVE the embed call"


@pytest.mark.asyncio
async def test_embed_task_outline_failure_never_fails_the_embed(monkeypatch):
    from jobs.tasks.embed import embed_document_task

    called: list[str] = []

    async def _boom(entity_id):
        raise RuntimeError("outline exploded")

    async def _reembed_ok(*a, **k):
        called.append("reembed")

    monkeypatch.setattr("doc_outline.refresh_outline", _boom)
    monkeypatch.setattr("embeddings._reembed", _reembed_ok)
    monkeypatch.setattr("embeddings._on_embed_success", _noop_success)
    await embed_document_task({"job_try": 1}, "doc", "emb-doc-2", "p1")

    assert called == ["reembed"], "an outline failure is log+continue"
