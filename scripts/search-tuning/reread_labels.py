"""Re-read labelling probe — runs INSIDE the backend container. Never writes to the DB.

Question it answers: when the in-app agent reads a document from the start AGAIN in
the same chat with no own edit of that document in between, is it because it LOST the
content (compaction) or for some other reason (a new user turn, someone else changed the
document, the first read was truncated, an in-context recheck)?

Why the conversation, not the session: `X-Agent-Session-Id` is the Lore chat id of the
turn (harness-driver/plugin/src/turn.ts loreId). A compaction mints a continuation chat
(`compacted_from` = source, backend/driver/compaction.py) and later turns carry ITS id,
so a re-read across a compaction falls into a different session_id. Pairs are therefore
built per conversation (source + its continuations), and a pair whose two reads sit on
different sides of a continuation's `created_at` is labelled `across_compaction`.

Labels, first match wins (a pair may also carry secondary flags):
  across_compaction  a continuation of the conversation was minted between the reads
  external_change    a checkpoint of the document by anyone was created between them
  first_truncated    the earlier read was truncated (next_offset present)
  new_user_turn      a user message in the conversation lies between the reads
  same_turn          none of the above — re-read inside one turn, content still in context

Usage:  python reread_labels.py [--since 2026-08-01] [--samples 20]
Output: JSON on stdout — label counts, gap distributions, and sample pairs (from the
sessions with the most reads) with the tool calls between the two reads, for hand review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, "/app")

from db import get_db  # noqa: E402

MUTATING = {
    "append_to_document", "edit_document", "edit_table_cell", "add_table_column",
    "add_table_rows", "create_table", "rename_document", "move_document",
    "reprocess_reference",
}


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


async def _calls(db, since: str) -> list[dict]:
    rows = await db.query(
        "SELECT created_at, entity_id, detail.tool AS tool, detail.session_id AS sid, "
        "detail.args AS args, detail.truncated AS truncated, detail.total_chars AS total, "
        "detail.call_id AS call_id, detail.outcome AS outcome "
        "FROM telemetry_event WHERE category = 'agent_tool' AND created_at >= <datetime>$since "
        "AND detail.session_id != NONE ORDER BY created_at",
        {"since": since},
    ) or []
    for r in rows:
        r["created_at"] = _ts(r["created_at"])
    return rows


async def _conversation_roots(db, sids: set[str]) -> tuple[dict[str, str], dict[str, list[datetime]]]:
    """sid -> root sid, and root -> sorted continuation mint times."""
    rows = await db.query(
        "SELECT meta::id(id) AS sid, compacted_from, created_at FROM chat_sessions "
        "WHERE compacted_from != NONE",
    ) or []
    parent = {r["sid"]: r["compacted_from"] for r in rows}
    minted: dict[str, list[datetime]] = defaultdict(list)

    def root(s: str) -> str:
        seen = set()
        while s in parent and s not in seen:
            seen.add(s)
            s = parent[s]
        return s

    for r in rows:
        minted[root(r["sid"])].append(_ts(r["created_at"]))
    for k in minted:
        minted[k].sort()
    return {s: root(s) for s in sids | set(parent)}, minted


async def _user_turns(db, chat_ids: list[str]) -> dict[str, list[datetime]]:
    rows = await db.query(
        "SELECT chat_id, created_at FROM messages WHERE role = 'user' AND chat_id IN $ids",
        {"ids": chat_ids},
    ) or []
    out: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        out[r["chat_id"]].append(_ts(r["created_at"]))
    return out


async def _checkpoints(db, doc_ids: list[str]) -> dict[str, list[tuple[datetime, str]]]:
    rows = await db.query(
        "SELECT document_id, created_at, created_by FROM checkpoints WHERE document_id IN $ids",
        {"ids": doc_ids},
    ) or []
    out: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for r in rows:
        out[r["document_id"]].append((_ts(r["created_at"]), str(r.get("created_by") or "")))
    return out


def _between(times, a: datetime, b: datetime) -> int:
    return sum(1 for t in times if a < (t[0] if isinstance(t, tuple) else t) < b)


def _arg_key(c: dict) -> str:
    return json.dumps(c.get("args") or {}, sort_keys=True, default=str)


def _arg_diff(a: dict, b: dict) -> dict:
    x, y = a.get("args") or {}, b.get("args") or {}
    return {k: [x.get(k), y.get(k)] for k in sorted(set(x) | set(y)) if x.get(k) != y.get(k)}


def _offset0(args) -> bool:
    return not (args or {}).get("offset")


def _pairs(calls: list[dict], root_of: dict[str, str]):
    """Yield (root, first_idx, second_idx, conv_calls) for re-reads from offset 0."""
    by_conv: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        by_conv[root_of.get(c["sid"], c["sid"])].append(c)
    for root, seq in by_conv.items():
        last_read: dict[str, int] = {}
        for i, c in enumerate(seq):
            doc = c.get("entity_id")
            if not doc:
                continue
            if c["tool"] in MUTATING:
                last_read.pop(doc, None)
                continue
            if c["tool"] != "read_document" or not _offset0(c.get("args")):
                continue
            if doc in last_read:
                yield root, last_read[doc], i, seq
            last_read[doc] = i


async def main(since: str, samples: int) -> None:
    db = await get_db()
    calls = await _calls(db, since)
    sids = {c["sid"] for c in calls}
    root_of, minted = await _conversation_roots(db, sids)
    conv_chats: dict[str, list[str]] = defaultdict(list)
    for s, r in root_of.items():
        conv_chats[r].append(s)
    turns_by_chat = await _user_turns(db, sorted(root_of))
    pairs = list(_pairs(calls, root_of))
    docs = sorted({seq[j]["entity_id"] for _, _, j, seq in pairs})
    cps = await _checkpoints(db, docs)

    labelled = []
    for root, i, j, seq in pairs:
        a, b = seq[i], seq[j]
        t1, t2, doc = a["created_at"], b["created_at"], b["entity_id"]
        turns = [t for chat in conv_chats[root] for t in turns_by_chat.get(chat, [])]
        flags = {
            "across_compaction": _between(minted.get(root, []), t1, t2) > 0,
            "external_change": _between(cps.get(doc, []), t1, t2) > 0,
            "first_truncated": bool(a.get("truncated")),
            "new_user_turn": _between(turns, t1, t2) > 0,
        }
        label = next((k for k, v in flags.items() if v), "same_turn")
        labelled.append({
            "root": root, "doc": doc, "label": label,
            "flags": [k for k, v in flags.items() if v],
            "gap_calls": j - i - 1, "gap_s": int((t2 - t1).total_seconds()),
            "same_session": a["sid"] == b["sid"],
            "total_chars": b.get("total"),
            "same_args": _arg_key(a) == _arg_key(b),
            "args_diff": _arg_diff(a, b),
            "first_error": a.get("outcome") == "error",
            "between": [f"{c['tool']}:{(c.get('entity_id') or '')[:8]}" for c in seq[i + 1:j]][:25],
        })

    reads_per_root = Counter(root_of.get(c["sid"], c["sid"]) for c in calls if c["tool"] == "read_document")
    top_roots = [r for r, _ in reads_per_root.most_common(15)]
    sample = labelled if samples < 0 else [p for p in labelled if p["root"] in top_roots][:samples]

    def gaps(label: str) -> dict:
        g = sorted(p["gap_calls"] for p in labelled if p["label"] == label)
        return {"n": len(g), "median_calls": g[len(g) // 2] if g else None,
                "p90_calls": g[int(len(g) * 0.9)] if g else None}

    print(json.dumps({
        "since": since, "calls": len(calls), "pairs": len(labelled),
        "labels": Counter(p["label"] for p in labelled),
        "flag_counts": Counter(f for p in labelled for f in p["flags"]),
        "cross_session_pairs": sum(1 for p in labelled if not p["same_session"]),
        "gaps": {lab: gaps(lab) for lab in ("across_compaction", "external_change",
                                            "first_truncated", "new_user_turn", "same_turn")},
        "same_args_by_label": {lab: sum(1 for p in labelled if p["label"] == lab and p["same_args"])
                               for lab in ("same_turn", "new_user_turn", "first_truncated")},
        "first_error_by_label": Counter(p["label"] for p in labelled if p["first_error"]),
        "diff_keys": Counter(k for p in labelled for k in p["args_diff"]),
        "same_turn_gap_s": sorted(p["gap_s"] for p in labelled if p["label"] == "same_turn")[::13],
        "samples": sample,
    }, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01T00:00:00Z")
    ap.add_argument("--samples", type=int, default=20)
    args = ap.parse_args()
    asyncio.run(main(args.since, args.samples))
