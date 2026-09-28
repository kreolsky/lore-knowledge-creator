"""Arm transports: the two ways the same executors are reached.

Both arms must present ONE dispatch signature to the loop — otherwise the harness
would branch per arm and the comparison would measure the harness.
"""
import json

import pytest

from evals.tool_ergonomics import transports
from evals.tool_ergonomics.harness import ToolError


class FakeHttp:
    def __init__(self, response=None, status=200, error_body=None):
        self.response = response
        self.status = status
        self.error_body = error_body
        self.sent = []

    def post(self, url, body, headers):
        self.sent.append({"url": url, "body": body, "headers": headers})
        if self.status >= 400:
            raise transports.HttpStatus(self.status, self.error_body or "")
        return self.response


def _mcp_ok(payload):
    return {"result": {"content": [{"text": json.dumps(payload)}], "isError": False}}


# ─── MCP arm ─────────────────────────────────────────────────────────────────

def test_mcp_dispatch_sends_jsonrpc_tools_call_with_the_bearer_token():
    http = FakeHttp(_mcp_ok({"id": "d1"}))
    dispatch = transports.mcp_dispatch(http, "http://h:8001", "tok")
    dispatch("read_document", {"document_id": "d1"})
    sent = http.sent[0]
    assert sent["url"] == "http://h:8001/mcp"
    assert sent["body"]["method"] == "tools/call"
    assert sent["body"]["params"] == {"name": "read_document",
                                      "arguments": {"document_id": "d1"}}
    assert sent["headers"]["Authorization"] == "Bearer tok"


def test_mcp_dispatch_unwraps_the_content_envelope():
    http = FakeHttp(_mcp_ok({"id": "d1", "title": "T"}))
    dispatch = transports.mcp_dispatch(http, "http://h:8001", "tok")
    assert dispatch("read_document", {})["title"] == "T"


def test_mcp_is_error_becomes_a_tool_error_carrying_the_payload_status():
    """A refusal arrives INSIDE a 200 envelope on this arm — reading only the HTTP
    status would score every refusal as a success."""
    http = FakeHttp({"result": {
        "content": [{"text": json.dumps({"status_code": 422, "error": "parent_id required"})}],
        "isError": True}})
    dispatch = transports.mcp_dispatch(http, "http://h:8001", "tok")
    with pytest.raises(ToolError) as exc:
        dispatch("create_document", {})
    assert exc.value.status == 422
    assert "parent_id required" in exc.value.detail


def test_mcp_transport_level_http_error_becomes_a_tool_error():
    http = FakeHttp(status=429, error_body="rate limited")
    dispatch = transports.mcp_dispatch(http, "http://h:8001", "tok")
    with pytest.raises(ToolError) as exc:
        dispatch("read_document", {})
    assert exc.value.status == 429


# ─── Pi arm ──────────────────────────────────────────────────────────────────

def test_agent_dispatch_posts_to_the_per_tool_endpoint():
    http = FakeHttp({"id": "d1"})
    dispatch = transports.agent_dispatch(http, "http://h:8001", "tok", mutating=set())
    dispatch("import_file", {"sandbox_path": "/x"})
    sent = http.sent[0]
    assert sent["url"] == "http://h:8001/api/tool/import_file"
    assert sent["body"] == {"sandbox_path": "/x"}
    assert sent["headers"]["Authorization"] == "Bearer tok"


def test_import_file_is_mutating_so_the_default_dispatch_injects_apply():
    """Pinned because the test above passes an empty set to isolate the URL — the
    real default must still treat import_file as the write it is."""
    assert "import_file" in transports.mutating_tool_names()


def test_agent_dispatch_injects_apply_for_a_mutating_tool():
    """Mirrors what the Pi driver does at the retired line-A driver's server.ts:239. Without it
    the body's `apply` defaults to confirm (models/tools.py:113) and every write
    becomes a proposal the model can neither see nor apply — a configuration that
    never occurs in production, so measuring it would measure the harness."""
    http = FakeHttp({"ok": True})
    dispatch = transports.agent_dispatch(http, "http://h:8001", "tok",
                                      mutating={"create_document"})
    dispatch("create_document", {"title": "T"})
    assert http.sent[0]["body"] == {"title": "T", "apply": "auto"}


def test_agent_dispatch_does_not_inject_apply_for_a_read_tool():
    """The driver injects it only for mutating tools; a read endpoint has no such
    field and would 422 on the extra key."""
    http = FakeHttp({"ok": True})
    dispatch = transports.agent_dispatch(http, "http://h:8001", "tok",
                                      mutating={"create_document"})
    dispatch("read_document", {"document_id": "d1"})
    assert http.sent[0]["body"] == {"document_id": "d1"}


def test_the_mutating_set_is_derived_from_the_backend_not_hand_listed():
    """A literal roster here would drift with the code it mirrors. The set the
    harness injects over must BE agent.tools.MUTATING_TOOLS, which
    driver/client.py:107 names as the single source of truth."""
    from agent.tools import MUTATING_TOOLS

    assert transports.mutating_tool_names() == set(MUTATING_TOOLS)


def test_agent_dispatch_raises_tool_error_on_a_4xx_with_the_detail():
    http = FakeHttp(status=422, error_body=json.dumps({"detail": "parent_id required"}))
    dispatch = transports.agent_dispatch(http, "http://h:8001", "tok")
    with pytest.raises(ToolError) as exc:
        dispatch("create_document", {})
    assert exc.value.status == 422
    assert exc.value.detail == "parent_id required"


def test_agent_dispatch_keeps_a_non_json_error_body_verbatim():
    """Swallowing an unparseable error would hide a 500's stack in the one place
    the write-up needs it."""
    http = FakeHttp(status=500, error_body="<html>boom</html>")
    dispatch = transports.agent_dispatch(http, "http://h:8001", "tok")
    with pytest.raises(ToolError) as exc:
        dispatch("edit_document", {})
    assert exc.value.detail == "<html>boom</html>"


# ─── Interchangeability ──────────────────────────────────────────────────────

def test_both_arms_expose_the_same_dispatch_signature():
    """The loop calls dispatch(name, args) and must not know which arm it is on."""
    http = FakeHttp({"ok": True})
    for factory in (transports.mcp_dispatch, transports.agent_dispatch):
        dispatch = factory(FakeHttp(_mcp_ok({"ok": True})), "http://h:8001", "t")
        assert callable(dispatch)
    assert http.sent == []
