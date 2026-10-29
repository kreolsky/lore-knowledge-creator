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


def test_consecutive_duplicate_headings_collapsed():
    content = "# T\n## А\n## А\n## Б\n## А"
    outline, hidden = _outline("T", content)
    assert outline == "А · Б · А"  # H1==title dropped; only CONSECUTIVE dupes collapse
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


def test_single_overlong_heading_drops_to_empty_and_counts():
    long_h1 = "# " + "Ж" * 40
    outline, hidden = _outline("T", long_h1, max_chars=10)
    assert outline == ""
    assert hidden == 1


# ─── Embed-job write path: outline refresh ABOVE _reembed ────────────────────


async def _noop_success(project_id):
    return None


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
