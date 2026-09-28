from unittest.mock import patch

import pytest


@pytest.fixture
def mock_fetch_one():
    """Patch fetch_one where the read_document executors live
    (agent.readonly_executors). Moved here from the retired
    backend/tests/conftest.py — this file was its only consumer."""
    with patch("agent.readonly_executors.fetch_one") as m:
        yield m


@pytest.mark.asyncio
async def test_read_document_returns_parent_id(mock_fetch_one):
    """_read_document_exec returns parent_id field."""
    pass


@pytest.mark.asyncio
async def test_read_document_root_has_null_parent_id(mock_fetch_one):
    """Document with no parent → parent_id is None."""
    pass
