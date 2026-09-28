"""Unit tests for Pydantic request models — validation rules, edge cases."""

import pytest
from pydantic import ValidationError

from models import (
    CreateApiKey,
    CreateUserAdmin,
    LoginRequest,
    RenameApiKey,
    SetProjectMember,
    UpdateProfileEmail,
    UpdateProfilePassword,
)


class TestLoginRequest:
    def test_valid(self):
        m = LoginRequest(email="user@example.com", password="secret")
        assert m.email == "user@example.com"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="not-an-email", password="secret")

    def test_empty_email_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="", password="secret")

    def test_password_max_length(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="u@e.com", password="x" * 201)

    def test_password_at_max_length(self):
        m = LoginRequest(email="u@example.com", password="x" * 200)
        assert len(m.password) == 200


class TestCreateUserAdmin:
    def test_valid_with_defaults(self):
        m = CreateUserAdmin(name="Alice", email="a@b.com", password="123456")
        assert m.role == "user"

    def test_password_too_short(self):
        with pytest.raises(ValidationError):
            CreateUserAdmin(name="Alice", email="a@b.com", password="12345")

    def test_name_too_long(self):
        with pytest.raises(ValidationError):
            CreateUserAdmin(name="x" * 101, email="a@b.com", password="123456")

    def test_role_must_be_valid_literal(self):
        with pytest.raises(ValidationError):
            CreateUserAdmin(name="A", email="a@b.com", password="123456", role="superadmin")

    def test_admin_role_accepted(self):
        m = CreateUserAdmin(name="A", email="a@b.com", password="123456", role="admin")
        assert m.role == "admin"


class TestUpdateProfilePassword:
    def test_valid(self):
        m = UpdateProfilePassword(current_password="old", new_password="newpass")
        assert m.new_password == "newpass"

    def test_new_password_too_short(self):
        with pytest.raises(ValidationError):
            UpdateProfilePassword(current_password="old", new_password="12345")


class TestUpdateProfileEmail:
    def test_valid(self):
        m = UpdateProfileEmail(email="new@e.com", current_password="pass")
        assert m.email == "new@e.com"

    def test_invalid_email(self):
        with pytest.raises(ValidationError):
            UpdateProfileEmail(email="bad", current_password="pass")


class TestSetProjectMember:
    def test_valid_access_levels(self):
        for level in ("full", "commentator", "readonly"):
            m = SetProjectMember(user_id="u1", access_level=level)
            assert m.access_level == level

    def test_invalid_access_level(self):
        with pytest.raises(ValidationError):
            SetProjectMember(user_id="u1", access_level="editor")


class TestRenameApiKey:
    def test_whitespace_stripped(self):
        m = RenameApiKey(label="  my key  ")
        assert m.label == "my key"

    def test_empty_after_strip_rejected(self):
        with pytest.raises(ValidationError):
            RenameApiKey(label="   ")

    def test_too_long(self):
        with pytest.raises(ValidationError):
            RenameApiKey(label="x" * 201)


class TestCreateApiKey:
    def test_defaults(self):
        m = CreateApiKey(document_id="doc-1")
        assert m.label == ""
        # INVARIANT: a user-issued key never expires unless an expiry is asked for.
        # Why: the 90-day default silently killed working widget keys (fix b7dc79c);
        # this assertion is the pin, so a re-introduced default fails here.
        assert m.expires_in_days is None

    def test_custom_expiry(self):
        m = CreateApiKey(document_id="doc-1", expires_in_days=30)
        assert m.expires_in_days == 30

    def test_no_expiry(self):
        m = CreateApiKey(document_id="doc-1", expires_in_days=None)
        assert m.expires_in_days is None
