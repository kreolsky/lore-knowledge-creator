"""Plan 1786230000000-memory-flatten-to-facts, Order 2 — the schema is the contract,
the validator is the second line.

Every test here binds the TWO surfaces — the `oneOf` branches in
APPLY_MEMORY_VERDICTS_TOOL and `memory/apply._validate_verdict` — so they cannot
drift apart. The action list is DERIVED from the schema's oneOf, never a literal:
a fifth action fails these tests until it is covered.

Order 2 collapsed the verdict surface to one fact level: `new` creates a fact-doc from
`title` + `text`; `merge` / `supersede` target a fact-doc by `fact_id`. The
entity-level fields (entity_id, entity_type, alias, target_claim_id) are gone.
"""

import jsonschema
from agent_tools.specs.memory import APPLY_MEMORY_VERDICTS_TOOL

_ITEM_SCHEMA = (
    APPLY_MEMORY_VERDICTS_TOOL["function"]["parameters"]
    ["properties"]["verdicts"]["items"]
)


def _schema_actions() -> list[str]:
    """The action list, derived from the oneOf branches — not hand-copied."""
    return [
        branch["properties"]["action"]["const"] for branch in _ITEM_SCHEMA["oneOf"]
    ]


def _schema_accepts(verdict: dict) -> bool:
    try:
        jsonschema.validate(verdict, _ITEM_SCHEMA)
        return True
    except jsonschema.ValidationError:
        return False


def _validator_accepts(verdict: dict) -> bool:
    from memory.apply import _validate_verdict

    return _validate_verdict(verdict) is None


# A known-good verdict per action — the minimal member of each branch.
_VALID = {
    "new": {"action": "new", "title": "Леон: хирург", "text": "х"},
    "merge": {"action": "merge", "fact_id": "fact-1"},
    "supersede": {"action": "supersede", "fact_id": "fact-1",
                  "reason": "устарело", "text": "у"},
    "skip": {"action": "skip", "reason": "эфемерно"},
}

# A verdict per action that BOTH surfaces must refuse — each violates exactly the
# branch's own required-field rule (and no other rule).
_INVALID = {
    "new": {"action": "new", "title": "Леон: хирург"},
    "merge": {"action": "merge"},
    "supersede": {"action": "supersede", "fact_id": "fact-1", "text": "у"},
    "skip": {"action": "skip"},
}


def test_the_schema_covers_every_action_the_validator_knows():
    from memory.apply import _ACTIONS

    assert sorted(_schema_actions()) == sorted(_ACTIONS), (
        "the schema's oneOf and the validator's action set drifted apart"
    )


def test_every_action_has_a_branch_requiring_its_own_fields():
    for action in _schema_actions():
        valid = _VALID[action]
        assert _schema_accepts(valid), f"schema rejected a valid {action}: {valid}"
        assert _validator_accepts(valid), (
            f"validator rejected a schema-valid {action}: {valid}"
        )
        invalid = _INVALID[action]
        assert not _schema_accepts(invalid), (
            f"schema accepted an invalid {action}: {invalid}"
        )
        assert not _validator_accepts(invalid), (
            f"validator accepted a schema-invalid {action}: {invalid}"
        )


def test_new_requires_title_and_text():
    """`new` creates a fact-doc — it needs both a subject-led title and a body."""
    assert _schema_accepts({"action": "new", "title": "t", "text": "х"})
    assert not _schema_accepts({"action": "new", "text": "х"})
    assert not _schema_accepts({"action": "new", "title": "t"})
    assert not _validator_accepts({"action": "new", "text": "х"})


def test_merge_keeps_its_text_exemption():
    """`merge` needs no text — it absorbs material into stored wording."""
    verdict = {"action": "merge", "fact_id": "fact-1"}
    assert _schema_accepts(verdict)
    assert _validator_accepts(verdict)


def test_supersede_inherits_title_when_omitted():
    """A supersede rewrites a fact in place; its title is optional (inherited)."""
    verdict = {"action": "supersede", "fact_id": "fact-1", "reason": "r", "text": "у"}
    assert _schema_accepts(verdict)
    assert _validator_accepts(verdict)


def test_fact_id_targeting_is_rejected_when_labelled_new():
    """Regression shape: a `new` carrying `fact_id` + no text is a mislabelled merge.
    The validator must name the correction — an error that does not discriminate the
    next attempt from the last one is the loop's engine."""
    verdict = {"action": "new", "fact_id": "fact-1"}
    assert not _schema_accepts(verdict)

    from memory.apply import _validate_verdict

    message = _validate_verdict({"action": "new", "fact_id": "fact-1"})
    assert message is not None
    assert 'action "merge"' in message, message


def test_rejection_messages_name_the_correction_not_only_the_rule():
    """Every inferred-intent shape gets an actionable message; the fallback is a
    bare rule only when nothing is inferable."""
    from memory.apply import _validate_verdict

    supersede_shape = _validate_verdict({
        "action": "merge", "fact_id": "f", "text": "х", "reason": "заменено",
    })
    # merge with text + reason IS a valid merge (text optional, reason ignored) —
    # no message expected.
    assert supersede_shape is None

    bare = _validate_verdict({"action": "new", "title": "t"})
    assert bare == "new requires text"


def test_the_batch_ceiling_is_a_guard_not_a_target():
    """D2 (plan 1786250000000): the verdict cap is a TRUNCATION GUARD, not a batch-size
    target. It sits well above any plausible single-reference batch (a full reference
    yields low tens of facts), the `maxItems` guard stays on (a batch that does not fit
    is refused, not truncated), and the description must NOT advertise a small fixed size
    as the norm — that is the quota (five per reference, one apply each) this run removes.

    Binds config (`MEMORY_VERDICTS_MAX_PER_BATCH`) against the tool schema's `maxItems`
    and description so the two cannot drift back into '5 is the target'."""
    from config import MEMORY_VERDICTS_MAX_PER_BATCH

    verdicts_schema = (
        APPLY_MEMORY_VERDICTS_TOOL["function"]["parameters"]
        ["properties"]["verdicts"]
    )
    assert verdicts_schema["maxItems"] == MEMORY_VERDICTS_MAX_PER_BATCH
    assert MEMORY_VERDICTS_MAX_PER_BATCH > 20
    description = verdicts_schema["description"].lower()
    assert "at most" not in description
    assert "several small calls" not in description
    assert "reference" in description
