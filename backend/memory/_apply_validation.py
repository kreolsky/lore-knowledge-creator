"""Verdict validation + pre-flight (the pure collecting pass).

Leaf of apply: the per-verdict validators and the collecting pre-flight. No DB, no
persist, no compensation — apply re-exports the white-box _validate_verdict and
_ACTIONS, and calls _preflight_verdicts / _tally_verdicts.

# ARCH: memory — verdict validation. Order 2 collapsed the model to one fact
# level, so the verdict surface is fact-addressed: `new` creates a fact-doc from a
# `title` + `text`, and `merge` / `supersede` target a fact-doc by `fact_id`. The
# entity-level addressing (entity_id, entity_type, alias, find-or-create) is gone.
"""
from __future__ import annotations

_ACTIONS = ("new", "merge", "supersede", "skip")



def _infer_correction(v: dict) -> str:
    """Name the CORRECTION, not the violation.

    An error that does not discriminate the next attempt from the last one turns
    any single bad verdict into an unbounded loop. Generic fallback only when
    nothing is inferable.
    """
    action = v.get("action")
    has_text = bool((v.get("text") or "").strip())
    if action != "merge" and v.get("fact_id") and not has_text:
        return (
            ", but you sent fact_id and no text — that is a merge (absorbing material "
            'into a stored fact); use action "merge" with fact_id'
        )
    if action != "supersede" and v.get("fact_id") and has_text and v.get("reason"):
        return (
            ", but you sent fact_id + text + reason — that is a supersede (replacing a "
            'stored fact); use action "supersede" with fact_id + reason + text'
        )
    return ""



def _validate_verdict(v: dict) -> str | None:
    """One verdict → the rejection message, or None when it is applicable.

    # INVARIANT: the per-action field rules live TWICE, in lock-step — the tool
    # schema's `oneOf` branches (agent/tools.py, the one surface the
    # model can inspect) and here (defence in depth). They state the SAME rules.  Why: the rules are enforced both in the JSON schema the model sees AND in code, so a model that ignores the schema is still blocked (defense in depth); lock-step keeps the schema from drifting from the code.
    # Why: a schema-valid object earning a 400 is a "no" the model cannot inspect,
    # so it guesses, and guessing retries the same bytes.
    # tests/backend/test_memory_verdict_schema.py derives the action list from the
    # schema's oneOf and binds both surfaces so they cannot drift apart.
    """
    action = v.get("action")
    if action not in _ACTIONS:
        return f"Unknown verdict action: {action}"
    hint = _infer_correction(v)
    if action == "skip":
        if not (v.get("reason") or "").strip():
            return f"skip requires reason{hint}"
        return None
    if action == "new":
        if not (v.get("text") or "").strip():
            return f"new requires text{hint}"
        if not (v.get("title") or "").strip():
            return f"new requires title (the fact's subject-led name){hint}"
        return None
    if action == "merge":
        if not (v.get("fact_id") or "").strip():
            return f"merge requires fact_id{hint}"
        absorb = (v.get("absorb_id") or "").strip()
        if absorb and absorb == (v.get("fact_id") or "").strip():
            return (
                "absorb_id must differ from fact_id — a fact cannot fold into "
                "itself; to reword ONE fact use action supersede"
            )
        return None
    # supersede
    if not (v.get("fact_id") or "").strip():
        return f"supersede requires fact_id{hint}"
    if not (v.get("reason") or "").strip():
        return f"supersede requires reason{hint}"
    if not (v.get("text") or "").strip():
        return f"supersede requires text (the corrected fact body){hint}"
    return None



def _tally_verdicts(valid: list[dict]) -> tuple[dict[str, int], list[dict]]:
    """`(actions, skipped)` over the pre-flighted valid verdicts.

    `actions` counts per verdict type (the report's `N× action` line); `skipped`
    records the agent's own skip decisions for the report. Reported, never silently
    dropped: with no pending state, the run's report is the only place a rejected
    item outlives the transcript.
    """
    actions = dict.fromkeys(_ACTIONS, 0)
    skipped: list[dict] = []
    for v in valid:
        actions[v["action"]] += 1
        if v["action"] == "skip":
            skipped.append({"text": v.get("text"), "reason": v.get("reason")})
    return actions, skipped



def _preflight_verdicts(
    verdicts: list[dict], extra_errors: dict[int, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Pass 0 — validate every verdict (collecting). Returns `(valid, rejected)`.

    `__memory_index` is stamped on every verdict so a runtime refusal (the duplicate
    gate, decided outside this pure pass) can report the verdict's ORIGINAL index.

    `extra_errors` carries rejections decided OUTSIDE this pure pass — today the
    duplicate gate (memory/dedup.py, needs embeddings) and the staleness gate
    (memory/_apply_resolution.py, unresolvable fact addresses). Every reason, from
    any gate, gets the SAME `verdict {index}:` prefix here — no gate embeds the
    index itself, so the rejection format cannot drift between gates.

    # ARCH: validation is a COLLECTING pass, not fail-fast. Valid verdicts apply;
    # invalid ones come back as per-verdict rejections the model corrects
    # INCREMENTALLY — an all-or-nothing 400 forces a full re-emit, and the re-emit
    # is what overflows the output cap.
    """
    errors: dict[int, str] = {}
    for index, reason in (extra_errors or {}).items():
        errors[index] = f"verdict {index}: {reason}"
    for i, v in enumerate(verdicts):
        v["__memory_index"] = i
        if i in errors:
            continue
        error = _validate_verdict(v)
        if error:
            errors[i] = f"verdict {i}: {error}"
    rejected = [
        {
            "index": i,
            "action": verdicts[i].get("action"),
            "reason": errors[i],
        }
        for i in sorted(errors)
    ]
    valid = [v for i, v in enumerate(verdicts) if i not in errors]
    return valid, rejected



__all__ = [
    "_ACTIONS", "_validate_verdict", "_preflight_verdicts", "_tally_verdicts",
]
