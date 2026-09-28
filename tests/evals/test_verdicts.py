"""Per-scenario judge contracts.

These judges already produced a confident wrong answer once: with the reference
read-back inverted, the control model's CORRECT calls were scored as three surface
defects. So each judge is pinned on both the pass and the fail it must distinguish.
"""
from evals.tool_ergonomics import verdicts
from evals.tool_ergonomics.harness import SessionLog, ToolCall
from evals.tool_ergonomics.scenarios import HIGHLIGHT, SLICE_A


class FakeFx:
    working_doc = "wd1"
    seed_child = "c1"
    seed_child_title = "Разбор второй тренировки 3"


def _case(key):
    return next(s for s in SLICE_A if s.key == key)


def _log(calls=(), budget=False):
    return SessionLog(task="t", arm="mcp", model="m", calls=list(calls),
                      budget_class=budget)


def _call(tool, args=None):
    return ToolCall(tool=tool, arguments=args or {}, raw_arguments="{}",
                    prompt_tokens=1, elapsed_s=0.0)


def _ref(id_, parent, title=""):
    return {"id": id_, "parent_id": parent, "node_type": "reference", "title": title}


def _doc(id_, parent, title=""):
    return {"id": id_, "parent_id": parent, "node_type": "document", "title": title}


# ─── a_origin ────────────────────────────────────────────────────────────────

def test_origin_passes_on_one_reference_hosted_by_the_open_document():
    out = verdicts.judge(_case("a_origin"), _log(), [_ref("r1", "wd1")], FakeFx(), "")
    assert out.passed is True


def test_origin_fails_when_the_reference_is_hosted_elsewhere():
    out = verdicts.judge(_case("a_origin"), _log(), [_ref("r1", "other")], FakeFx(), "")
    assert out.passed is False
    assert "elsewhere" in out.reason


def test_origin_fails_on_a_duplicate_created_by_a_retry():
    out = verdicts.judge(_case("a_origin"), _log(),
                         [_ref("r1", "wd1"), _ref("r2", "wd1")], FakeFx(), "")
    assert out.passed is False


# ─── b_placement_unstated ────────────────────────────────────────────────────

def test_null_host_is_caught_from_the_CALL_even_if_the_server_refused_it():
    """The misread is the model passing null — whether the server then accepted it
    is a separate question, and scoring only the end state would miss the refused
    attempt entirely."""
    calls = [_call("create_document", {"node_type": "reference", "parent_id": None})]
    out = verdicts.judge(_case("b_placement_unstated"), _log(calls), [], FakeFx(), "")
    assert out.passed is False
    assert "null" in out.reason


def test_a_document_with_a_null_parent_is_not_a_null_host():
    """node_type=document with parent_id=null is the documented way to say
    "project root" — flagging it would make the judge fire on correct behaviour."""
    calls = [_call("create_document", {"node_type": "document", "parent_id": None})]
    out = verdicts.judge(_case("b_placement_unstated"), _log(calls), [], FakeFx(), "")
    assert out.passed is True


def test_an_orphan_reference_in_the_end_state_fails_even_with_no_such_call():
    out = verdicts.judge(_case("b_placement_unstated"), _log(),
                         [_ref("r1", None)], FakeFx(), "")
    assert out.passed is False


# ─── c_kind_conversion ───────────────────────────────────────────────────────

def test_conversion_passes_when_the_same_id_is_now_a_reference():
    out = verdicts.judge(_case("c_kind_conversion"), _log(),
                         [_ref("c1", "wd1")], FakeFx(), "")
    assert out.passed is True


def test_conversion_fails_when_the_node_is_still_a_document():
    out = verdicts.judge(_case("c_kind_conversion"), _log(),
                         [_doc("c1", "wd1")], FakeFx(), "")
    assert out.passed is False
    assert "still a tree document" in out.reason


def test_delete_and_recreate_is_a_distinct_failure_from_a_vanished_node():
    """Both leave the id absent; only one of them is the model taking a worse
    route, and the write-up needs to tell them apart."""
    recreated = verdicts.judge(
        _case("c_kind_conversion"), _log(),
        [_ref("r9", "wd1", "Разбор второй тренировки 3")], FakeFx(), "")
    vanished = verdicts.judge(_case("c_kind_conversion"), _log(), [], FakeFx(), "")
    assert recreated.passed is False and "delete+recreate" in recreated.reason
    assert vanished.passed is False and "disappeared" in vanished.reason


# ─── d_anchor_drift ──────────────────────────────────────────────────────────

def test_anchor_drift_passes_only_when_the_edit_landed_and_the_span_survived():
    good = f"Разминка пятнадцать минут.\n\n{HIGHLIGHT}\n"
    assert verdicts.judge(_case("d_anchor_drift"), _log(), [], FakeFx(), good).passed


def test_anchor_drift_fails_when_the_edit_landed_but_destroyed_the_span():
    wrecked = "Разминка пятнадцать минут.\n\n#ff8800важное замечание\n"
    out = verdicts.judge(_case("d_anchor_drift"), _log(), [], FakeFx(), wrecked)
    assert out.passed is False
    assert "destroyed" in out.reason


def test_anchor_drift_fails_when_the_edit_never_landed():
    out = verdicts.judge(_case("d_anchor_drift"), _log(), [],
                         FakeFx(), f"Разминка десять минут.\n{HIGHLIGHT}")
    assert out.passed is False


def test_an_unreadable_document_fails_rather_than_passing_silently():
    out = verdicts.judge(_case("d_anchor_drift"), _log(), [], FakeFx(), None)
    assert out.passed is False


# ─── cross-cutting ───────────────────────────────────────────────────────────

def test_a_route_only_scenario_has_no_pass_fail_but_keeps_its_route():
    out = verdicts.judge(_case("e_protocol_fork"),
                         _log([_call("attach_file")]), [], FakeFx(), "")
    assert out.passed is None
    assert out.route == ["attach_file"]


def test_budget_class_is_carried_onto_every_outcome():
    out = verdicts.judge(_case("a_origin"), _log(budget=True), [], FakeFx(), "")
    assert out.excluded_as_budget is True


def test_no_judge_reads_the_models_narration():
    """Structural check on the INVARIANT: a session whose final_text claims success
    must score identically to one that says nothing."""
    quiet = SessionLog(task="t", arm="mcp", model="m")
    loud = SessionLog(task="t", arm="mcp", model="m",
                      final_text="Готово, я приложил справку к документу!")
    case = _case("a_origin")
    assert (verdicts.judge(case, quiet, [], FakeFx(), "").passed
            == verdicts.judge(case, loud, [], FakeFx(), "").passed is False)
