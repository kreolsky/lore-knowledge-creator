"""Documents command-layer structure tests.

Pins the split of routes/documents.py into thin HTTP handlers + the
backend/documents/ command layer: each handler enforces access then
delegates, and the cross-route batch-delete (routes/references.py) reaches
the COMMAND module, not a route module. Behavior contracts (status codes,
events, payload shapes) are pinned by the HTTP-surface suite
(test_documents*.py, test_documents_batch.py, test_batch_delete_event.py);
these tests pin the STRUCTURE so a regression to fat handlers is visible.
"""

from unittest.mock import AsyncMock, patch

from routes.documents import (
    get_documents_batch,
)

from models import (
    DocumentBatchRequest,
)

# ─── handlers delegate to the command layer ──────────────────────────────────


async def test_batch_fetch_delegates_to_command():
    body = DocumentBatchRequest(ids=["d1", "d2"])
    user = {"user_id": "u1"}
    with patch("routes.documents.fetch_content_batch",
               new_callable=AsyncMock, return_value={"items": []}) as cmd:
        result = await get_documents_batch(body, user=user)
    cmd.assert_awaited_once_with(["d1", "d2"], user)
    assert result == {"items": []}
