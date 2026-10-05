"""Render one search-metrics snapshot, or diff two of them. Host-side, stdlib only.

    compare.py --show  snapshots/dev-scifi-baseline.json
    compare.py --diff  snapshots/dev-scifi-baseline.json snapshots/dev-scifi-after-s1.json
    compare.py --scores snapshots/xling-bench-giga.json

--scores reads the score cuts off one snapshot: where the CORRECT answer sat on this
model's cosine scale (for RETRIEVAL_MIN_SCORE) and the steepest step between consecutive
hits of its kind from the top down to it (for RETRIEVAL_SCORE_DROP_OFF). Capture that snapshot with both cuts at
0.0, otherwise it only shows the answers the current cuts already let through.

The diff reports RANK movement per query, not score movement: changing what text gets
embedded moves every cosine value, so scores are not comparable across snapshots while
ranks are.

A diff across two different MEASUREMENT CONFIGS (score cuts, top_k, budgets) is refused
the same way a diff across two corpora is: a rank that moved because top_k widened reads
exactly like a rank that moved because the model improved. The embedding-model A/B is
exactly this hazard — every arm must be captured under one config, control included.
"""

from __future__ import annotations

import argparse
import json


def _load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _fmt_measurement(m: dict | None) -> str:
    if not m:
        return "measurement: (not recorded — snapshot predates the config block)"
    return (
        f"measurement: model={m.get('embedding_model')} "
        f"min_score={m.get('min_score')} drop_off={m.get('score_drop_off')} "
        f"top_k={m.get('top_k')} budget={m.get('budget_tokens')} "
        f"max_per_doc={m.get('max_per_doc')}"
    )


def show(snap: dict) -> None:
    cov = snap["coverage"]
    s = snap["summary"]
    print(
        f"set: {snap['set']}  project: {snap['project_id']}  at: {snap['captured_at']}  "
        f"corpus: {snap.get('corpus_fingerprint', '?')}"
    )
    print(
        f"coverage: {cov['indexed_share']} indexed "
        f"({cov['of_them_unindexed']}/{cov['documents_over_200_chars']} docs >200 chars "
        f"have NO chunks, {cov['unindexed_chars']} chars unindexed); "
        f"{cov['total_chunks']} chunks, {cov['stale_chunks_of_emptied_docs']} stale"
    )
    print(_fmt_measurement(snap.get("measurement")))
    print(
        f"MRR {s['mrr']}  hit@1 {s['hit_at_1']}/{s['queries']}  "
        f"hit@3 {s['hit_at_3']}/{s['queries']}  missed {s['missed']}"
    )
    for cls, c in sorted(s["by_class"].items()):
        print(f"  {cls:<11} n={c['n']}  hit@1={c['hit1']}  hit@3={c['hit3']}  miss={c['miss']}")
    print()
    for r in snap["results"]:
        rank = r.get("rank_in_kind", r.get("rank"))
        mark = "MISS" if rank is None else f"#{rank}"
        top = r["hits"][0]["title"] if r.get("hits") else "-"
        err = f"  ERROR={r['error']}" if r.get("error") else ""
        print(f"  [{r['class']:<10}] {mark:<5} {r['id']:<28} want={r['expect']:<22} top={top}{err}")


def diff(a: dict, b: dict) -> None:
    ra = {r["id"]: r for r in a["results"]}
    rb = {r["id"]: r for r in b["results"]}
    sa, sb = a["summary"], b["summary"]
    fa = a.get("corpus_fingerprint", "?")
    fb = b.get("corpus_fingerprint", "?")
    print(f"A: {a['captured_at']}   coverage {a['coverage']['indexed_share']}   corpus {fa}")
    print(f"B: {b['captured_at']}   coverage {b['coverage']['indexed_share']}   corpus {fb}")
    if fa != fb:
        # The dev DB is shared with other sessions and is reseeded on container
        # recreation. A corpus change makes every delta below ambiguous — it cannot be
        # attributed to the code change under test. Say so loudly rather than render a
        # comparison that reads as evidence.
        print()
        print("  !! CORPUS CHANGED between the two runs — these are NOT comparable.")
        print("     Deltas below mix the code change with documents added/edited/backfilled.")
        print("     Re-capture both snapshots on one corpus state before drawing a conclusion.")
    ma, mb = a.get("measurement"), b.get("measurement")
    print(f"A {_fmt_measurement(ma)}")
    print(f"B {_fmt_measurement(mb)}")
    # Everything except the model must match — the model is the thing under test.
    keys = ("min_score", "score_drop_off", "top_k", "budget_tokens", "max_per_doc")
    if ma and mb:
        moved = [k for k in keys if ma.get(k) != mb.get(k)]
        if moved:
            print()
            print(f"  !! MEASUREMENT CONFIG CHANGED ({', '.join(moved)}) — NOT comparable.")
            print("     A rank that moved because a knob moved reads like a model win.")
    elif ma or mb:
        print()
        print("  !! one snapshot has no measurement block — configs cannot be compared.")
    if a["coverage"]["indexed_share"] != b["coverage"]["indexed_share"]:
        print("  !! indexed share moved — a backfill ran between the runs")
    print(
        f"MRR {sa['mrr']} -> {sb['mrr']}   "
        f"hit@1 {sa['hit_at_1']} -> {sb['hit_at_1']}   "
        f"missed {sa['missed']} -> {sb['missed']}"
    )
    print()
    for qid in ra.keys() | rb.keys():
        x, y = ra.get(qid), rb.get(qid)
        if not x or not y:
            print(f"  {qid:<28} only in {'A' if x else 'B'}")
            continue
        pa = x.get("rank_in_kind", x.get("rank"))
        pb = y.get("rank_in_kind", y.get("rank"))
        if pa == pb:
            continue
        fmt = lambda p: "MISS" if p is None else f"#{p}"  # noqa: E731
        # Lower rank is better; None is worst.
        better = (pb is not None) and (pa is None or pb < pa)
        print(f"  {'+' if better else '-'} [{y['class']:<10}] {qid:<28} {fmt(pa)} -> {fmt(pb)}")


def _quantile(xs: list[float], q: float) -> float:
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def scores(snap: dict) -> None:
    m = snap.get("measurement") or {}
    print(_fmt_measurement(m))
    if m.get("min_score") or m.get("score_drop_off"):
        print("  !! score cuts are not 0.0 — answers the cuts dropped are missing below.")
    found, ratios = [], []
    for r in snap["results"]:
        expect = (r.get("expect") or "").lower()
        answer = next((h for h in r.get("hits", []) if expect and expect in h["title"].lower()), None)
        if answer is None:
            print(f"  MISS  {r['id']}")
            continue
        # WHY: drop-off runs per kind and stops at the FIRST cliff from the top
        # (retrieval._assemble_hits / _apply_drop_off), so the answer survives a cut
        # only if every step above it, within its own kind, clears it.
        chain = [h["score"] for h in r["hits"] if h["kind"] == answer["kind"]]
        i = chain.index(answer["score"])
        steps = [chain[j] / chain[j - 1] for j in range(1, i + 1) if chain[j - 1]]
        ratio = min(steps) if steps else None
        found.append(answer["score"])
        if ratio is not None:
            ratios.append(ratio)
        shown = f"{ratio:.3f}" if ratio is not None else "-"
        print(f"  #{i + 1:<3} score={answer['score']:.4f}  worst-step={shown:<6} "
              f"{answer['kind']:<9} {r['id']}")
    if not found:
        return
    found.sort()
    ratios.sort()
    print()
    print(f"correct-answer score: n={len(found)} min={found[0]:.4f} "
          f"p10={_quantile(found, 0.1):.4f} median={_quantile(found, 0.5):.4f}")
    print("  RETRIEVAL_MIN_SCORE above `min` drops a correct answer of this set.")
    if ratios:
        print(f"worst step above the answer: n={len(ratios)} min={ratios[0]:.3f} "
              f"p10={_quantile(ratios, 0.1):.3f}")
        print("  RETRIEVAL_SCORE_DROP_OFF above `min` cuts a correct answer of this set.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show")
    ap.add_argument("--diff", nargs=2)
    ap.add_argument("--scores")
    args = ap.parse_args()
    if args.show:
        show(_load(args.show))
    elif args.diff:
        diff(_load(args.diff[0]), _load(args.diff[1]))
    elif args.scores:
        scores(_load(args.scores))
    else:
        ap.error("pass --show, --diff or --scores")


if __name__ == "__main__":
    main()
