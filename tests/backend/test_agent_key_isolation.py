"""agent-key internals live in `agent.keys` and NOWHERE else —
the structural pin. The per-(user,project) get_or_create_agent_key + plaintext
cache died with the SSE pump (plan agent-line-harness-lifecycle step 9): keys are
now the STANDING per-chat-session rows (get_or_create_session_agent_key; behavior
covered in test_session_agent_keys.py). driver.client sources nothing from routes.*
anymore — the followup payload carries the session key minted by the chat routes.

Structural — does not touch the live Redis.
"""
import inspect


def test_driver_client_sources_nothing_from_routes():
    """The key-leak seam is closed at the source: driver.client (the thin
    HTTP/WS relay) touches NO routes.tool_api or agent-key surface — the
    standing session key rides the followup payload minted by the chat
    routes. (A lazy agent.tools import for the tool-set constants
    stays allowed: payload shape, not credentials.)"""
    import driver.client

    src = inspect.getsource(driver.client)
    assert "routes.tool_api" not in src, (
        "driver.client must not reach into routes.tool_api (the old _redis/"
        "get_or_create_agent_key private-import leak)"
    )
    assert "agent_keys" not in src and "agent.keys" not in src, (
        "driver.client must not source agent-key helpers — the payload carries"
        " the standing key minted by the chat routes"
    )
