"""Bare tool-calling loop contracts.

The loop measures how a model reads a tool surface. Every way a session can end
must be RECORDED and CLASSIFIED — a crash, a silent truncation, or an unlabelled
stop would each be read later as a surface finding when it is a budget artifact.
"""
import pytest

from evals.tool_ergonomics import harness


def _tool_call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args}}


def _model(*turns):
    """A fake model: yields the given assistant messages in order, then repeats
    the last one forever (so cap tests have something to cap)."""
    seq = list(turns)

    def call(messages, specs):
        idx = min(len(seq) - 1, sum(1 for m in messages if m["role"] == "assistant"))
        return {"message": seq[idx], "usage": {"prompt_tokens": 100 + idx}}
    return call


def _ok_dispatch(name, args):
    return {"ok": True, "tool": name, "args": args}


# ─── Termination ─────────────────────────────────────────────────────────────

def test_session_ends_when_the_model_stops_calling_tools():
    log = harness.run_session(
        task="do the thing", specs=[], model=_model({"role": "assistant", "content": "done"}),
        dispatch=_ok_dispatch)
    assert log.stop_reason == "model_stopped"
    assert log.calls == []


def test_call_cap_stops_a_looping_model_and_labels_it_budget():
    looper = _model({"role": "assistant", "tool_calls": [_tool_call("read_document", "{}")]})
    log = harness.run_session(task="t", specs=[], model=looper, dispatch=_ok_dispatch,
                              max_calls=15)
    assert log.stop_reason == "call_cap"
    assert len(log.calls) == 15
    assert log.budget_class is True


def test_wall_clock_stops_the_session_and_labels_it_budget():
    """A model that is slow rather than loopy must not run the series overnight."""
    def slow_dispatch(name, args):
        harness._CLOCK.advance(60.0)
        return {"ok": True}
    looper = _model({"role": "assistant", "tool_calls": [_tool_call("read_document", "{}")]})
    log = harness.run_session(task="t", specs=[], model=looper, dispatch=slow_dispatch,
                              max_calls=15, wall_clock_s=100.0)
    assert log.stop_reason == "wall_clock"
    assert log.budget_class is True
    assert len(log.calls) < 15


# ─── Failure classification ──────────────────────────────────────────────────

def test_invalid_json_arguments_are_recorded_as_budget_not_a_crash():
    """A weak model emitting malformed args is a budget failure, excluded from
    surface findings — but the raw text is kept verbatim for the write-up."""
    bad = _model({"role": "assistant",
                  "tool_calls": [_tool_call("read_document", "{not json")]})
    log = harness.run_session(task="t", specs=[], model=bad, dispatch=_ok_dispatch,
                              max_calls=3)
    assert log.calls[0].arg_parse_error is not None
    assert log.calls[0].raw_arguments == "{not json"
    assert log.budget_class is True


def test_a_dispatch_error_is_fed_back_to_the_model_not_raised():
    """Error recovery on the first retry is a measured scenario — the loop must
    hand the model its own 422 rather than aborting the session."""
    calls = {"n": 0}

    def failing(name, args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise harness.ToolError(422, "parent_id is required")
        return {"ok": True}

    model = _model(
        {"role": "assistant", "tool_calls": [_tool_call("create_document", "{}", "c1")]},
        {"role": "assistant", "tool_calls": [_tool_call("create_document", '{"parent_id":"d1"}', "c2")]},
        {"role": "assistant", "content": "done"},
    )
    log = harness.run_session(task="t", specs=[], model=model, dispatch=failing, max_calls=5)
    assert log.stop_reason == "model_stopped"
    assert log.calls[0].error == {"status": 422, "detail": "parent_id is required"}
    assert log.calls[1].error is None


def test_a_call_to_an_unserved_tool_is_recorded_as_a_surface_finding():
    """Inventing a tool name is exactly the misread the eval hunts — it is NOT
    budget-class, so it must not be filtered out of the findings."""
    model = _model({"role": "assistant", "tool_calls": [_tool_call("attach_to", "{}")]},
                   {"role": "assistant", "content": "done"})
    specs = [{"type": "function", "function": {"name": "create_document",
                                               "parameters": {"type": "object"}}}]
    log = harness.run_session(task="t", specs=specs, model=model, dispatch=_ok_dispatch,
                              max_calls=5)
    assert log.calls[0].unserved_tool is True
    assert log.budget_class is False


# ─── Logging ─────────────────────────────────────────────────────────────────

def test_every_call_is_logged_verbatim_with_tokens_and_elapsed():
    model = _model({"role": "assistant",
                    "tool_calls": [_tool_call("read_document", '{"document_id":"d1"}')]},
                   {"role": "assistant", "content": "done"})
    log = harness.run_session(task="t", specs=[], model=model, dispatch=_ok_dispatch,
                              max_calls=5)
    call = log.calls[0]
    assert call.tool == "read_document"
    assert call.arguments == {"document_id": "d1"}
    assert call.raw_arguments == '{"document_id":"d1"}'
    assert call.prompt_tokens == 100
    assert call.elapsed_s >= 0.0


def test_the_task_is_the_only_user_message_and_never_names_a_tool():
    """Naming a tool tests obedience, not discoverability — the harness refuses a
    task string that mentions a served tool name."""
    specs = [{"type": "function", "function": {"name": "create_document",
                                               "parameters": {"type": "object"}}}]
    with pytest.raises(ValueError, match="names a served tool"):
        harness.run_session(task="use create_document please", specs=specs,
                            model=_model({"role": "assistant", "content": "ok"}),
                            dispatch=_ok_dispatch)


def test_session_log_serializes_to_json_for_the_write_up():
    model = _model({"role": "assistant", "content": "done"})
    log = harness.run_session(task="t", specs=[], model=model, dispatch=_ok_dispatch)
    blob = log.to_dict()
    assert blob["stop_reason"] == "model_stopped"
    assert blob["task"] == "t"
    assert isinstance(blob["calls"], list)
