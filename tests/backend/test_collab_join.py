"""Collab WS client independence — one client entry per connection.

# WHY: The session registry keys clients by id(ws) — every WS connection
# joined to an entity is an independent client entry (two tabs of one user,
# two users). Dropping one connection must not affect the other. The per-entity
# channel that shared this registry is deleted (plan fewer-layers); the
# invariant lives on the one remaining project channel.
# Tests verify this invariant and guard against regression. Plan §P2-#5.
"""

import json

from collab.registry import _session_key, _sessions
from helpers import join_collab_ws, project_collab_url


class TestConnectionClientIndependence:
    """Two connections joined to the same entity on the project WS get two
    independent client entries. Dropping one leaves the other intact."""

    def test_drop_second_connection_keeps_first(self, sync_app, collab_project):
        """Closing the second connection leaves the first connection's client active."""
        pid, doc_id, admin_token, *_ = collab_project

        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws_first:
            join_collab_ws(ws_first, doc_id)
            init = json.loads(ws_first.receive_text())
            assert init["type"] == "init"

            with sync_app.websocket_connect(
                project_collab_url(pid), cookies={"lore_session": admin_token}
            ) as ws_second:
                join_collab_ws(ws_second, doc_id)
                assert json.loads(ws_second.receive_text())["type"] == "init"

                joined = json.loads(ws_first.receive_text())
                assert joined["type"] == "user_joined"

                session = _sessions[_session_key("doc", doc_id)]
                assert len(session.clients) == 2

            # ws_second closed by the with-exit; the survivor sees user_left.
            left = json.loads(ws_first.receive_text())
            assert left["type"] == "user_left"

            session = _sessions[_session_key("doc", doc_id)]
            assert len(session.clients) == 1

    def test_drop_first_connection_keeps_second(self, sync_app, collab_project):
        """Closing the first connection leaves the second connection's client active."""
        pid, doc_id, admin_token, *_ = collab_project

        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws_second:
            join_collab_ws(ws_second, doc_id)
            json.loads(ws_second.receive_text())  # init

            with sync_app.websocket_connect(
                project_collab_url(pid), cookies={"lore_session": admin_token}
            ) as ws_first:
                join_collab_ws(ws_first, doc_id)
                json.loads(ws_first.receive_text())  # init

                joined = json.loads(ws_second.receive_text())
                assert joined["type"] == "user_joined"

                session = _sessions[_session_key("doc", doc_id)]
                assert len(session.clients) == 2

            # ws_first closed by the with-exit; the survivor sees user_left.
            left = json.loads(ws_second.receive_text())
            assert left["type"] == "user_left"

            session = _sessions[_session_key("doc", doc_id)]
            assert len(session.clients) == 1

    def test_session_cleanup_after_last_disconnect(self, sync_app, collab_project):
        """Session clients list empties when the last client disconnects."""
        pid, doc_id, admin_token, *_ = collab_project

        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            json.loads(ws.receive_text())
            session = _sessions[_session_key("doc", doc_id)]
            assert len(session.clients) == 1

        assert len(session.clients) == 0

    def test_two_users_two_connections(self, sync_app, collab_project):
        """Two different users on two connections each get a client entry."""
        pid, doc_id, admin_token, user_token, admin_uid, user_uid = collab_project

        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws_admin:
            join_collab_ws(ws_admin, doc_id)
            json.loads(ws_admin.receive_text())

            with sync_app.websocket_connect(
                project_collab_url(pid), cookies={"lore_session": user_token}
            ) as ws_user:
                join_collab_ws(ws_user, doc_id)
                assert json.loads(ws_user.receive_text())["type"] == "init"

                joined = json.loads(ws_admin.receive_text())
                assert joined["type"] == "user_joined"
                assert joined["user"]["user_id"] == user_uid

                session = _sessions[_session_key("doc", doc_id)]
                assert len(session.clients) == 2
                user_ids = {c.user_id for c in session.clients.values()}
                assert user_ids == {admin_uid, user_uid}
