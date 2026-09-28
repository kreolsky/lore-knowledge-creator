"""Unit tests for models.resolve_message_author (Stage 2)."""

from models import resolve_message_author


def test_returns_author_id_when_set():
    assert resolve_message_author({"author_id": "alice"}, {"user_id": "bob"}) == "alice"


def test_falls_back_to_session_user_id_when_missing():
    assert resolve_message_author({}, {"user_id": "bob"}) == "bob"


def test_falls_back_when_author_id_is_none():
    assert resolve_message_author({"author_id": None}, {"user_id": "bob"}) == "bob"


def test_empty_string_author_falls_back():
    # Falsy semantics: empty string -> use session owner. Matches access.py.
    assert resolve_message_author({"author_id": ""}, {"user_id": "bob"}) == "bob"
