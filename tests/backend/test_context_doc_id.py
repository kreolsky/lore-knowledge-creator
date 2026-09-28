"""Context scope lines embed the document id (plan §5, collapse step 2).

Context pins scope, not bodies: each pinned doc/reference contributes a
{role: system} line carrying title + id + a read_document steer. Both the agent
Pi injection consume the same ctx.system_prefix, so verifying the formatter
output covers it.
"""
from routes.chat.completions_turn import _flatten_system_prefix_to_text
from routes.chat.context import _format_doc, _format_ref_doc


def test_format_doc_scope_line_carries_id_not_body():
    doc = {"title": "Lore Doc", "content": "body text", "is_reference": False}
    prefix: list[dict] = []
    _format_doc(doc, "doc-42", "proj-1", prefix, [])
    text = prefix[0]["content"]
    assert "Lore Doc" in text and "doc-42" in text
    assert "body text" not in text
    assert "read_document" in text


def test_format_doc_empty_content_same_scope_line():
    """Empty or not, the pinned doc contributes the same scope line — emptiness
    is body knowledge the prompt no longer carries."""
    doc = {"title": "Empty Doc", "content": "", "is_reference": False}
    prefix: list[dict] = []
    _format_doc(doc, "doc-7", "proj-1", prefix, [])
    text = prefix[0]["content"]
    assert "Empty Doc" in text and "doc-7" in text
    assert "read_document" in text


async def test_format_ref_doc_scope_line_carries_id_not_body():
    ref = {
        "title": "Ref Material",
        "content": "ref body",
        "is_reference": True,
        "media_type": "markdown",
    }
    prefix: list[dict] = []
    await (_format_ref_doc(ref, "ref-9", "proj-1", prefix, []))
    text = prefix[0]["content"]
    assert "Ref Material" in text and "ref-9" in text
    assert "ref body" not in text
    assert "read_document" in text


def test_agent_flattened_text_carries_id():
    """The agent Pi injection (_flatten_system_prefix_to_text) keeps the id."""
    doc = {"title": "Agent Doc", "content": "agent body", "is_reference": False}
    prefix: list[dict] = []
    _format_doc(doc, "agent-doc-1", "proj-1", prefix, [])
    flat = _flatten_system_prefix_to_text(prefix)
    assert "agent-doc-1" in flat
