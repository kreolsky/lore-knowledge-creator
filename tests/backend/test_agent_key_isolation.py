"""agent-key internals live in `agent.keys` and NOWHERE else —
the structural pin. The per-(user,project) get_or_create_agent_key + plaintext
cache died with the SSE pump (plan agent-line-harness-lifecycle step 9): keys are
now the STANDING per-chat-session rows (get_or_create_session_agent_key; behavior
covered in test_session_agent_keys.py). driver.client sources nothing from routes.*
anymore — the followup payload carries the session key minted by the chat routes.

Structural — does not touch the live Redis.
"""
import inspect


def test_agent_keys_module_exposes_expected_public_names():
    """The key functions move to their own module (single home)."""
    import importlib

    mod = importlib.import_module("agent.keys")
    for name in (
        "create_agent_key_row",
        "mint_run_key",
        "get_or_create_session_agent_key",
        "revoke_session_agent_key",
        "revoke_member_session_keys",
        "AGENT_KEY_LABEL",
        "AGENT_KEY_DEFAULT_TTL_DAYS",
    ):
        assert hasattr(mod, name), f"agent.keys missing {name!r}"


def test_tool_api_no_longer_owns_key_internals():
    """tool_api keeps the /keys ROUTE (boundary) but the key internals moved out —
    no _redis / get_or_create_agent_key / _create_agent_key_row defined there."""
    import routes.tool_api as tool_api

    for name in (
        "_redis",
        "get_or_create_agent_key",
        "_create_agent_key_row",
        "_agent_key_cache_get",
        "_agent_key_cache_set",
    ):
        assert not hasattr(tool_api, name), (
            f"tool_api must not own {name!r} anymore (moved to agent.keys)"
        )


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
