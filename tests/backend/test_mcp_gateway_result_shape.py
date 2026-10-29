"""Slice 1 — result ergonomics: canonical ids + structuredContent/outputSchema.

Two pure-serialization guarantees over the MCP gateway output boundary:
  #2 canonical proposal_id — every id the gateway emits is a plain uuid (no
     SurrealDB record wrapping `table:⟨uuid⟩`). Normalized once at the
     dispatch output boundary, never per-tool.
  #3 structuredContent + outputSchema — `_ok()` emits BOTH the legacy text block
     (back-compat for older clients) AND a `structuredContent` dict; every tool
     declares `Tool.outputSchema` so capable clients get typed results.

Mirrors the parity contract: the text fallback must stay byte-identical so the
readonly/mutating/init/work-product suites stay green (regression guard #4).
"""


import pytest

try:
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover — optional validator
    jsonschema = None


# ─── #2b: a nested RecordID in a result is normalized recursively (F5) ─────────


@pytest.mark.asyncio
async def test_nested_record_id_is_canonicalized(monkeypatch):
    """coerce_record_ids coerces a RecordID nested inside a list/dict, not just at
    the top level (F5 audit — type-based recursive normalization). The helper now
    lives in db.py (single record-id coercion, alongside extract_id); the gateway
    output boundary (_ok) calls it."""
    from db import coerce_record_ids

    class _FakeRecordID:
        # Duck-typed to match db.is_record_id's name check.
        def __init__(self, uuid):
            self._uuid = uuid

        def __str__(self):
            return f"documents:`{self._uuid}`"

    _FakeRecordID.__name__ = "RecordID"
    nested = {"documents": [{"id": _FakeRecordID("abc-123"), "title": "x"}], "top": "plain"}
    out = coerce_record_ids(nested)
    assert out["documents"][0]["id"] == "abc-123", "nested RecordID not normalized"
    assert out["top"] == "plain", "a plain string must be untouched"
