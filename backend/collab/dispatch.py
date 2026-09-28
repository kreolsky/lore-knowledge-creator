"""Collab `selection` control message → the advisory region-lock registry."""
from __future__ import annotations

import logging

from collab.session import ConnectedClient

logger = logging.getLogger(__name__)


def handle_selection_message(
    msg: dict, client: ConnectedClient, *, default_entity_id: str | None,
) -> None:
    """Track (or clear) the client's active selection in the region-lock registry.

    # ARCH: the selection is a best-effort, advisory input to the
    # region-lock pre-apply check — never applied to or persisted in the Y.Doc.
    # Offsets are code points (consistent with the agent apply path). A missing or
    # non-integer payload is ignored: a malformed selection must not corrupt the
    # registry or wedge the WS loop.
    """
    from collab import selection_registry as reg

    entity_id = msg.get("entity_id") or default_entity_id
    if not entity_id:
        return
    try:
        from_cp = int(msg.get("from_cp"))  # type: ignore[arg-type]
        to_cp = int(msg.get("to_cp"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return
    reg.track(entity_id, client.user_id, client.user_name, from_cp, to_cp)
