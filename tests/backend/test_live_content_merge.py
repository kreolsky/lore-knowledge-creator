"""Tests for the shared live-session content merge helper (Item 3).

Replaces the two duplicated "prefer live collab Y.Doc over the DB record" gates
that lived in routes.chat.context and agent.
"""

from types import SimpleNamespace
from unittest.mock import patch

from collab.events import merge_live_content


def test_merge_live_content_overrides_with_live_session():
    """Active collab session content wins over the DB record."""
    doc = {"id": "documents:d1", "content": "Old DB body", "title": "T"}
    live = SimpleNamespace(content="Fresh live body")

    with patch("collab.registry.get_active_session", return_value=live):
        merged = merge_live_content(doc, "d1")

    assert merged["content"] == "Fresh live body"
    assert merged["title"] == "T"


def test_merge_live_content_noop_without_live_session():
    """No active session → DB record returned unchanged."""
    doc = {"id": "documents:d1", "content": "DB body", "title": "T"}

    with patch("collab.registry.get_active_session", return_value=None):
        merged = merge_live_content(doc, "d1")

    assert merged is doc
    assert merged["content"] == "DB body"
