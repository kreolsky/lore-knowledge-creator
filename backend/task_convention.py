"""# SYSTEM: task-convention — the tasks feature's convention half: schema
resolve, next_task selection, claim_task, set_task_status.

Tasks are documents with properties plus a convention on top: the properties half (the header grammar, the
projection, find_documents, the visibility decision) is doc_properties.py;
THIS module owns what the convention adds — which document is the schema,
which task is next, and the two writes the loop makes (the claim and the
status transition, which archives a closed top-level task).

# ARCH (the schema is a document, validated at READ time): a visible doc whose
# header has `type: schema, schema_for: task` names the statuses, the
# ready/claimed/closed anchors, the optional initial status, the optional
# priority order (most urgent FIRST — index 0 is served first) and the
# optional `archive:` document closed top-level tasks move under; its BODY
# is the project's own task rules (log heading, definition of done), read
# by the agent, never parsed here. Editing the
# schema therefore needs no re-projection of every task — next_task /
# claim_task re-validate it on every call. Zero visible schema docs → 409
# {code: no_schema} naming the expected header; two or more → 409 listing
# their ids (no silent pick); an anchor outside `statuses` → 409 naming the
# field.

# ARCH (claim is coordination, not access control): any read-write key can
# still edit the header with edit_document — the claim only records WHO works
# the task and until when. The mutual exclusion is the cross-replica
# doc_edit_lock around the read-decide-write (the same Redis lock every agent
# edit apply path takes), plus the header text itself as the verdict: the
# claim decision is made on the LIVE text (resolve_live_doc_state — never
# `props`, which lags the ~1 s flush plus the projection subscriber), so a
# task claimed a moment ago answers 409 already_claimed even though
# next_task (a `props` reader) may still have offered it.

# ARCH (lease is lazy): TASK_CLAIM_LEASE hours, renewed by calling claim_task
# again with the same agent_name. Expiry is decided per read — next_task
# offers a stale claim, claim_task takes it over — so there is no cron and
# the server never edits content without an actor. The human escape hatch is
# editing `status` by hand: the claim is void the moment status != claimed,
# with no cleanup state anywhere.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from agent.doc_state import resolve_live_doc_state, route_document_edits
from agent.tool_api_surface import create_agent_pre_edit_checkpoint
from doc_edit_lock import edit_lock
from documents.move import _resolve_move_destination, move_document_command
from documents.service import sibling_rows
from fastapi import HTTPException
from page_lock import locked_doc_ids

from db import fetch_one, get_ancestor_ids, get_descendant_ids
from db.records import SAFE_ID_RE
from doc_properties import FRONTMATTER_RE, parse_header
from mentions import extract_content_edges
from scope import require_doc_in_scope

#: The claim lease. A bare constant, not a setting: nobody can meaningfully
#: choose it (the same stance as DOC_EDIT_LOCK_TTL_S).
TASK_CLAIM_LEASE = timedelta(hours=4)

#: `agent_name` is written INTO the task header, so a newline or `:` would
#: inject header keys — the grammar is validated before anything is read.
#: `\w` is Unicode on str patterns: any script's letters and digits pass,
#: while `:`, newlines and bidi/format controls do not.
AGENT_NAME_RE = re.compile(r"^[^\W_][\w .@-]{0,63}$")

#: A top-level header line `key: value` — unindented only, so a `status:`
#: inside a nested mapping is not the convention's key.
_HEADER_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")

#: Cap on each reported list of next_task (invalid / blocked / reserved /
#: in_progress) — the counts carry the full size.
_REPORT_CAP = 20

#: YAML value shapes that are NOT a scalar — the claim write refuses them
#: under its keys instead of guessing a serialization.
_NON_SCALAR_PREFIXES = ("[", "{", "|", ">")

#: The no_schema refusal text — the expected header, spelled once.
_NO_SCHEMA_DETAIL = (
    "No task schema document is visible to this key. Create one: a document "
    "whose YAML header has `type: schema`, `schema_for: task`, `statuses: "
    "[<status>...]`, `ready: <status>`, `claimed: <status>`, `closed: "
    "[<status>...]` (optional: `initial: <status>`, `priority: [<value>...]` "
    "with the most urgent value FIRST — it is served first, `archive: "
    "<link to a document>` that closed top-level tasks move under), with the "
    "project's task rules in the body."
)


class HeaderLineError(ValueError):
    """The header holds a non-scalar value under a claim key (status /
    assignee / claimed_until) — the claim refuses rather than serialize a
    guess. A 409 at the handler, never a silent rewrite."""


def _refuse(code: str, detail: str, **extra) -> None:
    """The ONE refusal shape of the convention: 409 + {code, detail} — a
    project-state conflict (misconfigured schema, contested claim), not a
    malformed request."""
    raise HTTPException(
        status_code=409, detail={"code": code, "detail": detail, **extra},
    )


# ─── The schema document ──────────────────────────────────────────────────────


def _string_list(props: dict, field: str) -> list | None:
    """`field` as a non-empty list of strings, or None when absent. Refuses
    (invalid_schema) a present-but-wrong shape — the schema is the convention
    every task is validated against, so a half-parsed one must not serve."""
    value = props.get(field)
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(
        isinstance(v, str) and v for v in value
    ):
        _refuse(
            "invalid_schema",
            f"schema field `{field}` must be a non-empty list of strings",
        )
    return value


def _schema_status_field(props: dict, field: str, statuses: list) -> str:
    """`field` as a member of `statuses` — the ready/claimed/initial anchors."""
    value = props.get(field)
    if not isinstance(value, str):
        _refuse("invalid_schema", f"schema field `{field}` is missing")
    if value not in statuses:
        _refuse(
            "invalid_schema",
            f"schema field `{field}` ({value!r}) is not in `statuses`",
        )
    return value


def resolve_task_schema(rows: list[dict]) -> dict:
    """Find and validate the ONE task-schema document among the visible
    header-bearing `rows` (visible_header_docs output — the same visibility
    decision every property consumer uses).

    409 {no_schema | ambiguous_schema | invalid_schema}; returns the fields.
    """
    candidates = [
        r for r in rows
        if isinstance(r.get("props"), dict)
        and r["props"].get("type") == "schema"
        and r["props"].get("schema_for") == "task"
    ]
    if not candidates:
        _refuse("no_schema", _NO_SCHEMA_DETAIL)
    if len(candidates) > 1:
        ids = ", ".join(r.get("id") or "?" for r in candidates)
        _refuse(
            "ambiguous_schema",
            f"{len(candidates)} visible documents claim to be the task "
            f"schema ({ids}) — keep exactly one.",
        )
    row = candidates[0]
    props = row["props"]
    statuses = _string_list(props, "statuses")
    if statuses is None:
        _refuse("invalid_schema", "schema field `statuses` is missing")
    closed = _string_list(props, "closed")
    if closed is None:
        _refuse("invalid_schema", "schema field `closed` is missing")
    for c in closed:
        if c not in statuses:
            _refuse("invalid_schema",
                    f"schema field `closed` has {c!r} not in `statuses`")
    initial = props.get("initial")
    if initial is None:
        initial = statuses[0]
    elif initial not in statuses:
        _refuse("invalid_schema",
                f"schema field `initial` ({initial!r}) is not in `statuses`")
    return {
        "schema_id": row.get("id"), "title": row.get("title"),
        "statuses": statuses,
        "ready": _schema_status_field(props, "ready", statuses),
        "claimed": _schema_status_field(props, "claimed", statuses),
        "closed": closed,
        "initial": initial if initial is not None else statuses[0],
        "priority": _string_list(props, "priority"),
        "archive_id": _schema_archive_id(props),
    }


def _schema_archive_id(props: dict) -> str | None:
    """The optional `archive:` document id (a Lore link or a bare id), None
    when absent; present but unresolvable refuses (invalid_schema)."""
    raw = props.get("archive")
    if raw is None:
        return None
    archive_id = _resolve_dep_id(raw)
    if archive_id is None:
        _refuse("invalid_schema",
                f"schema field `archive` ({raw!r}) is not a document link or id")
    return archive_id


# ─── next_task selection (pure over the visible rows + the schema) ───────────


def _parse_iso(value) -> datetime | None:
    """An ISO datetime from the grammar's normalized header value, or None.

    A naive datetime is UTC (the grammar's own rule). None for anything the
    caller should not act on: not a string, or unparseable — an unparseable
    `claimed_until` is treated as NOT past (the conservative answer: never
    silently take over a claim we cannot read)."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_dep_id(entry) -> str | None:
    """One `depends` entry → a document id: a Lore link (`[t](doc:id)` /
    `[t](id)`, read by the mentions tokenizer) or a bare SAFE_ID_RE id.
    None = unresolvable."""
    if not isinstance(entry, str):
        return None
    entry = entry.strip()
    if not entry:
        return None
    edges = extract_content_edges(entry)
    if edges:
        return edges[0][0]
    if SAFE_ID_RE.match(entry):
        return entry
    return None


def _dep_block_reason(props: dict, by_id: dict, closed: list) -> str | None:
    """Why `depends` blocks this task, or None when every dependency is
    closed. Unresolvable entries and invisible targets block with their own
    reasons — a dependency the caller cannot see is not satisfied. The
    caller has already refused a non-str/non-list shape (invalid, not
    blocked)."""
    deps = props.get("depends")
    if deps is None:
        return None
    entries = [deps] if isinstance(deps, str) else deps
    for entry in entries:
        dep_id = _resolve_dep_id(entry)
        if dep_id is None:
            return f"dependency unresolvable: {entry!r}"
        dep = by_id.get(dep_id)
        if dep is None:
            return f"dependency not visible: {dep_id}"
        dep_status = (dep.get("props") or {}).get("status")
        if dep_status not in closed:
            return f"dependency not closed: {dep_id} (status {dep_status!r})"
    return None


def _task_view(row: dict, stale: bool = False) -> dict:
    out = {
        "id": row.get("id"), "title": row.get("title"),
        "parent_id": row.get("parent_id"), "props": row.get("props"),
    }
    if stale:
        out["stale"] = True
    return out


def _classify_task(
    row: dict, schema: dict, now: datetime, agent_name: str | None,
) -> tuple[str, object]:
    """One visible row's offer state: ("invalid", reason | error),
    ("skip", None) — not a task / not this loop's turn —, ("in_progress",
    None) — an active claim —, ("reserved", assignee) — a ready task assigned
    to someone other than `agent_name` (any assignee when it is None) — or
    ("available", stale: bool)."""
    error = row.get("props_error")
    props = row.get("props")
    if error:
        return "invalid", error
    if not isinstance(props, dict) or props.get("type") != "task":
        return "skip", None
    status = props.get("status")
    if not isinstance(status, str):
        return "invalid", "missing status"
    if status not in schema["statuses"]:
        return "invalid", f"unknown status {status!r}"
    stale = False
    if status == schema["ready"]:
        assignee = props.get("assignee")
        if isinstance(assignee, str) and assignee and assignee != agent_name:
            return "reserved", assignee
    elif status == schema["claimed"]:
        until = _parse_iso(props.get("claimed_until"))
        if until is None or until > now:
            return "in_progress", None  # never offered, listed for resume
        stale = True
    else:
        return "skip", None  # closed / intermediate status: not this turn
    return "available", stale


def _malformed_field(props: dict, priorities: list | None) -> str | None:
    """Why a field the selection reads has a shape it cannot honor, or None.

    A malformed field is REPORTED in `invalid`, never silently dropped or
    coerced: a dropped `depends` would offer a task its author blocked, an
    unhashable `priority` would 500 the selection for every task in the
    project, and a typo'd priority sorted last would demote urgent work with
    no signal (no-silent-degradation). A schema without a `priority` list
    opts out of ordering, so a task's priority is then not checked."""
    deps = props.get("depends")
    if deps is not None and not isinstance(deps, (str, list)):
        return "depends must be a string or a list of strings"
    priority = props.get("priority")
    if isinstance(priority, (list, dict)):
        return "priority must be a single value"
    if priorities and priority is not None and priority not in priorities:
        return f"unknown priority {priority!r}"
    return None


def _scan_rows(
    rows: list[dict], schema: dict, by_id: dict, now: datetime,
    agent_name: str | None,
) -> tuple[dict[str, list[dict]], list[tuple[dict, bool]]]:
    """One pass over the visible rows → ({invalid, blocked, reserved,
    in_progress}, available)."""
    report: dict[str, list[dict]] = {
        "invalid": [], "blocked": [], "reserved": [], "in_progress": [],
    }
    invalid, blocked = report["invalid"], report["blocked"]
    available: list[tuple[dict, bool]] = []
    for row in rows:
        state, payload = _classify_task(row, schema, now, agent_name)
        rid, title = row.get("id"), row.get("title")
        if state == "invalid":
            invalid.append({"id": rid, "title": title, "reason": payload})
        elif state == "reserved":
            report["reserved"].append(
                {"id": rid, "title": title, "assignee": payload})
        elif state == "in_progress":
            props = row["props"]
            report["in_progress"].append({
                "id": rid, "title": title,
                "assignee": props.get("assignee"),
                "claimed_until": props.get("claimed_until"),
            })
        elif state == "available":
            shape_error = _malformed_field(row["props"], schema["priority"])
            if shape_error is not None:
                invalid.append({"id": rid, "title": title, "reason": shape_error})
                continue
            reason = _dep_block_reason(row["props"], by_id, schema["closed"])
            if reason is not None:
                blocked.append({"id": rid, "title": title, "reason": reason})
            else:
                available.append((row, payload))
    return report, available


def select_next_task(
    rows: list[dict], schema: dict, *, now: datetime,
    lookup: list[dict] | None = None, agent_name: str | None = None,
) -> dict:
    """The next_task answer over the candidate `rows`: the first AVAILABLE task
    (ready, or a claim whose lease has lapsed, with every dependency closed),
    ordered by the task's `priority` value in the schema's `priority` list
    (missing = last) then `created_at` (the rows' own order — the sort is
    stable), plus the counts and the capped invalid/blocked reports.

    A broken header is REPORTED in `invalid` (it may be a typo'd task — the
    no-silent-drop posture of find_documents' header_errors), never offered.

    `lookup` is every row visible to the key (default: `rows`) — dependencies
    resolve against it, not against the candidates: a task narrowed by
    `under` may depend on a task outside that subtree, and it is satisfied
    when that task is closed.

    A ready task carrying an `assignee` other than `agent_name` is reserved:
    never offered, listed in `reserved`. An actively claimed task is listed
    in `in_progress` so its holder can resume it.
    """
    by_id = {r.get("id"): r for r in (rows if lookup is None else lookup)}
    report, available = _scan_rows(rows, schema, by_id, now, agent_name)
    prio_order = {p: i for i, p in enumerate(schema["priority"] or [])}
    available.sort(
        key=lambda pair: prio_order.get(
            (pair[0].get("props") or {}).get("priority"), len(prio_order),
        ),
    )
    return {
        "task": _task_view(*available[0]) if available else None,
        "schema_id": schema["schema_id"],
        "initial": schema["initial"],
        "counts": {
            "available": len(available),
            **{name: len(items) for name, items in report.items()},
            "stale": sum(1 for _row, stale in available if stale),
        },
        **{name: items[:_REPORT_CAP] for name, items in report.items()},
    }


# ─── The header writes (one splice over the header block) ────────────────────


def splice_header_keys(
    block: str, values: dict[str, str], drop: tuple[str, ...] = (),
) -> str:
    """The header block with each `values` key's line replaced by `key: value`
    (appended at the block's end — before the closing fence — when absent)
    and each `drop` key's line removed. Values are written as given: the
    caller serializes them.

    Top-level lines only (an indented `status:` belongs to a nested mapping).
    A non-scalar value under a touched key raises HeaderLineError — the
    caller refuses with 409 and writes nothing.
    """
    trailing = "\n" if block.endswith("\n") else ""
    core = block[:-1] if trailing else block
    lines = core.split("\n") if core else []
    touched = (*values, *drop)
    seen: list[str] = []
    out: list[str] = []
    for line in lines:
        m = _HEADER_KEY_LINE_RE.match(line)
        key = m.group(1) if m else None
        if key not in touched:
            out.append(line)
            continue
        value = m.group(2).strip()
        if any(value.startswith(p) for p in _NON_SCALAR_PREFIXES):
            raise HeaderLineError(
                f"header key `{key}` holds a non-scalar value; fix it by "
                f"hand before changing it",
            )
        if key in values:
            out.append(f"{key}: {values[key]}")
            seen.append(key)
    out.extend(f"{k}: {v}" for k, v in values.items() if k not in seen)
    return "\n".join(out) + trailing


def build_claimed_block(
    block: str, *, status: str, agent_name: str, claimed_until: str,
) -> str:
    """The claim's header block: `status:` replaced, `assignee:` /
    `claimed_until:` upserted, both double-quoted."""
    return splice_header_keys(block, {
        "status": status,
        "assignee": json.dumps(agent_name, ensure_ascii=False),
        "claimed_until": json.dumps(claimed_until),
    })


def _lease_verdict(props: dict, agent_name: str, now: datetime) -> str:
    """A claimed header's lease against `agent_name`: "own" (theirs) or
    "lapsed" (anyone may act), else 409 already_claimed naming the holder.
    An unparseable `claimed_until` is live (_parse_iso's conservative
    reading)."""
    assignee = props.get("assignee")
    until_raw = props.get("claimed_until")
    until = _parse_iso(until_raw)
    if assignee == agent_name:
        return "own"
    if until is not None and until <= now:
        return "lapsed"
    _refuse(
        "already_claimed",
        "This task is claimed by another agent; call next_task again "
        "for a different one.",
        assignee=assignee if isinstance(assignee, str) else None,
        claimed_until=until_raw if isinstance(until_raw, str) else None,
    )


def _decide_claim_outcome(
    props: dict, schema: dict, agent_name: str, now: datetime,
) -> str:
    """The claim verdict for a parsed task header: claimed / renewed /
    taken_over, or the 409 refusals (already_claimed with the holder named,
    reserved with the assignee, not_ready with the status). Pure — the
    caller owns the lock and the read."""
    status = props.get("status")
    if status == schema["claimed"]:
        verdict = _lease_verdict(props, agent_name, now)
        return {"own": "renewed", "lapsed": "taken_over"}[verdict]
    if status == schema["ready"]:
        assignee = props.get("assignee")
        if isinstance(assignee, str) and assignee and assignee != agent_name:
            _refuse(
                "reserved",
                "This task is assigned to someone else; call next_task again "
                "for a different one.",
                assignee=assignee,
            )
        return "claimed"
    _refuse(
        "not_ready",
        "This task's status is not the schema's ready or claimed status.",
        status=status if isinstance(status, str) else None,
    )


def _splice_or_refuse(
    block: str, values: dict[str, str], drop: tuple[str, ...], expected: dict,
) -> str:
    """splice_header_keys with the 409 mapping — a non-scalar under a touched
    key refuses and nothing is written.

    # WHY: the line edit is verified by re-parsing its result. A value under a
    # touched key that spans lines (`assignee:` + an indented `- x`, a folded
    # plain scalar) leaves its continuation lines behind after the one-line
    # replace and breaks the header; the parse of the new block must equal
    # `expected` (the old header with exactly the touched keys changed), or
    # the write refuses instead of writing a header it would itself report as
    # invalid.
    """
    try:
        new_block = splice_header_keys(block, values, drop)
    except HeaderLineError as exc:
        _refuse("non_scalar_claim_key", str(exc))
    if parse_header(f"---\n{new_block}---\n") != (expected, None):
        _refuse(
            "non_scalar_claim_key",
            "a status, assignee or claimed_until value spans several header "
            "lines; fix it by hand first",
        )
    return new_block


async def _read_live_task(document_id: str):
    """(live_text, tables_json, header match, props) of a task document, or
    409 not_a_task. Call under edit_lock."""
    live_text, tables_json = await resolve_live_doc_state(document_id)
    props, error = parse_header(live_text)
    if not isinstance(props, dict) or props.get("type") != "task":
        _refuse(
            "not_a_task",
            "This document's header does not say `type: task`"
            + (f" ({error})" if error else ""),
        )
    return live_text, tables_json, FRONTMATTER_RE.match(live_text), props


async def _splice_header(
    *, document_id: str, project_id: str, live_text: str, tables_json,
    match, new_block: str, preview: str,
) -> None:
    """Checkpoint, then ONE splice over the header block through
    route_document_edits."""
    await create_agent_pre_edit_checkpoint(
        document_id=document_id, content=live_text, tables_json=tables_json,
        original_preview=preview,
    )
    await route_document_edits(
        doc_id=document_id, project_id=project_id,
        edits=[{
            "from_cp": match.start(1), "to_cp": match.end(1),
            "new_text": new_block, "original_text": match.group(1),
        }],
    )


async def execute_claim(
    *, document_id: str, agent_name: str, project_id: str, schema: dict,
) -> dict:
    """The read-decide-write of claim_task, under the cross-replica
    edit_lock.

    The decision is made on the LIVE text (resolve_live_doc_state — the
    editor buffer or the persisted Y.Doc, never the `props` projection), the
    write is ONE splice over the header block through route_document_edits
    (the ONE surgical convergence path; it emits content_flushed, so the
    claim reaches `props` through the same projection subscriber), and the
    pre-edit checkpoint lands BEFORE the splice — the claim is reversible in
    History like every agent content write.
    """
    async with edit_lock(document_id):
        live_text, tables_json, match, props = await _read_live_task(document_id)
        now = datetime.now(timezone.utc)
        outcome = _decide_claim_outcome(props, schema, agent_name, now)
        claimed_until = (now + TASK_CLAIM_LEASE).isoformat()
        new_block = _splice_or_refuse(match.group(1), {
            "status": schema["claimed"],
            "assignee": json.dumps(agent_name, ensure_ascii=False),
            "claimed_until": json.dumps(claimed_until),
        }, (), {
            **props, "status": schema["claimed"], "assignee": agent_name,
            "claimed_until": claimed_until,
        })
        await _splice_header(
            document_id=document_id, project_id=project_id,
            live_text=live_text, tables_json=tables_json, match=match,
            new_block=new_block,
            preview=f"claim by {agent_name}: {match.group(0)[:60]}",
        )
    return {
        "outcome": outcome, "document_id": document_id,
        "assignee": agent_name, "claimed_until": claimed_until,
    }


# ─── The status transition (set_task_status) ─────────────────────────────────


def check_target_status(status: str, schema: dict) -> None:
    """409 unless `status` is a schema status other than the claimed one —
    the claim owns the lease and its mutual exclusion."""
    if status not in schema["statuses"]:
        _refuse("unknown_status", f"{status!r} is not in the schema's statuses",
                statuses=schema["statuses"])
    if status == schema["claimed"]:
        _refuse("use_claim_task",
                "The claimed status is set by claim_task, which owns the lease.")


def _is_task_row(row: dict | None) -> bool:
    props = (row or {}).get("props")
    return isinstance(props, dict) and props.get("type") == "task"


async def _archive_destination(
    document_id: str, schema: dict, by_id: dict,
) -> str | None:
    """The archive id a closing task moves under, or None: no `archive:`,
    already under it, or a subtask (a visible ancestor is a task).

    # WHY: only a top-level task is archived; a subtask closes in place —
    # its subtree goes to the archive with its parent — moving it out
    # alone destroys the hierarchy the parent's open-subtask check reads.
    """
    archive_id = schema["archive_id"]
    if archive_id is None:
        return None
    ancestors = (await get_ancestor_ids(document_id))[1:]
    if archive_id in ancestors or any(_is_task_row(by_id.get(a)) for a in ancestors):
        return None
    return archive_id


async def _refuse_open_subtasks(
    document_id: str, project_id: str, by_id: dict, closed: list,
) -> None:
    """409 open_subtasks when a visible descendant task is not closed.

    # WHY: reads `props`, which lags the live text by the flush plus the
    # projection; a subtask closed a second ago may still block. Accepted —
    # a spurious 409 is safe, a missed one is not; do not "fix" it by
    # reading every descendant's live text.
    """
    open_tasks = [
        {"id": did, "title": by_id[did].get("title"),
         "status": by_id[did]["props"].get("status")}
        for did in await get_descendant_ids(document_id, project_id)
        if _is_task_row(by_id.get(did))
        and by_id[did]["props"].get("status") not in closed
    ]
    if open_tasks:
        _refuse("open_subtasks", "Close the open subtasks first.",
                subtasks=open_tasks[:_REPORT_CAP])


async def _validate_archive(
    document_id: str, archive_id: str, project_id: str, user: dict,
    scope_root: str | None,
) -> None:
    """The archive is visible to the key and a valid destination for the
    task (cycle, parent validity, scope, memory boundary) — checked BEFORE
    the header splice, so a refusal writes nothing."""
    row = await fetch_one("documents", archive_id)
    visible = bool(
        row and not row.get("deleted_at") and row.get("project_id") == project_id
        and archive_id not in await locked_doc_ids(user["user_id"])
    )
    if visible and scope_root:
        try:
            await require_doc_in_scope(scope_root, archive_id)
        except HTTPException:
            visible = False
    if not visible:
        _refuse("archive_not_visible",
                "The schema's `archive` document is not visible to this key.",
                archive_id=archive_id)
    await _resolve_move_destination(
        await fetch_one("documents", document_id), document_id, archive_id,
        None, project_id, scope_root, None,
    )


async def _move_to_archive(
    document_id: str, archive_id: str, project_id: str, scope_root: str | None,
) -> None:
    """Move the closed task under the archive, LAST in its sibling group
    (after_id=None would land it at the top)."""
    siblings = [s for s in await sibling_rows(project_id, archive_id)
                if s["id"] != document_id]
    await move_document_command(
        document_id=document_id, parent_id=archive_id,
        after_id=siblings[-1]["id"] if siblings else None,
        project_id=project_id, scope_root=scope_root,
    )


async def _decide_transition(
    *, document_id: str, status: str, agent_name: str, props: dict,
    schema: dict, by_id: dict, project_id: str, user: dict,
    scope_root: str | None,
) -> tuple[bool, str | None]:
    """(header_change, archive_id) for a live task header, or the 409s:
    already_claimed, no_change, open_subtasks, archive_not_visible and the
    move destination's own refusals — all before anything is written."""
    previous = props.get("status")
    closing = status in schema["closed"]
    if previous == schema["claimed"]:
        _lease_verdict(props, agent_name, datetime.now(timezone.utc))
    archive_id = (
        await _archive_destination(document_id, schema, by_id) if closing else None
    )
    header_change = previous != status or "claimed_until" in props
    if not header_change and archive_id is None:
        _refuse("no_change", f"The task is already {status!r}.")
    if closing:
        await _refuse_open_subtasks(document_id, project_id, by_id, schema["closed"])
    if archive_id is not None:
        await _validate_archive(document_id, archive_id, project_id, user, scope_root)
    return header_change, archive_id


async def execute_set_status(
    *, document_id: str, status: str, agent_name: str, project_id: str,
    schema: dict, rows: list[dict], user: dict, scope_root: str | None,
) -> dict:
    """The read-decide-write of set_task_status, under the edit_lock.

    Decided on the LIVE header: a live foreign lease refuses
    (already_claimed); the write replaces `status:` and drops
    `claimed_until:` (`assignee:` stays as the record of who worked it).
    Closing refuses open visible subtasks and moves a top-level task under
    the schema's `archive`, validated before the splice. Same status with
    nothing left to do → 409 no_change; same closed status with a pending
    archive move → only the move, so a move that failed after the splice is
    completed by calling again.
    """
    by_id = {r.get("id"): r for r in rows}
    async with edit_lock(document_id):
        live_text, tables_json, match, props = await _read_live_task(document_id)
        previous = props.get("status")
        header_change, archive_id = await _decide_transition(
            document_id=document_id, status=status, agent_name=agent_name,
            props=props, schema=schema, by_id=by_id, project_id=project_id,
            user=user, scope_root=scope_root,
        )
        if header_change:
            expected = {k: v for k, v in props.items() if k != "claimed_until"}
            new_block = _splice_or_refuse(
                match.group(1), {"status": status}, ("claimed_until",),
                {**expected, "status": status},
            )
            await _splice_header(
                document_id=document_id, project_id=project_id,
                live_text=live_text, tables_json=tables_json, match=match,
                new_block=new_block,
                preview=f"status {previous} → {status} by {agent_name}",
            )
    if archive_id is not None:
        await _move_to_archive(document_id, archive_id, project_id, scope_root)
    return {
        "outcome": "set", "document_id": document_id, "status": status,
        "previous_status": previous, "archived_to": archive_id,
    }
