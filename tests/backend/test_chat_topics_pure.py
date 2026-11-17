"""chat topics — the answer parse and the window stitching, pure (no DB, no model)."""

from jobs.tasks.chat_topics import merge_window, parse_topics


# TEST-WHY: pure-logic-edge — a second window's equal title must join the frozen topic while the first window's unfrozen exchanges are relabelled, never kept twice.
def test_second_window_stitches_by_title_and_drops_stale_unfrozen_labels():
    order = ["m0", "m1", "m2", "m3"]
    first = parse_topics(
        '<think>x</think>[{"title": "Томаты", "synopsis": "s", "turns": [0, 7]}, '
        '{"title": "Цепь", "synopsis": "c", "turns": [2]}]',
        ["m0", "m1", "m2"],
    )
    # turn 7 is outside the window; m1 is uncovered and inherits m0's topic
    assert [(t["title"], t["message_ids"]) for t in first] == [
        ("Томаты", ["m0", "m1"]), ("Цепь", ["m2"])]
    empty = {"frozen_ids": [], "labelled_ids": [], "topics": []}
    toc = merge_window(empty, order, ["m0", "m1"], first[:1], freeze=True)
    toc = merge_window(toc, order, ["m2"], first[1:], freeze=False)
    assert toc["frozen_ids"] == ["m0", "m1"] and toc["labelled_ids"] == ["m0", "m1", "m2"]

    second = parse_topics('[{"title": "Томаты", "synopsis": "new", "turns": [1]}, '
                          '{"title": "Аквариум", "synopsis": "a", "turns": [0]}]', ["m2", "m3"])
    toc = merge_window(toc, order, ["m2", "m3"], second, freeze=False)
    assert [(t["title"], t["synopsis"], t["message_ids"], t["frozen"]) for t in toc["topics"]] == [
        ("Томаты", "s", ["m0", "m1", "m3"], False),
        ("Аквариум", "a", ["m2"], False),
    ]
    assert parse_topics("no array here", ["m0"]) == []


class _RowsDB:
    """Serves one chat's message rows to chat_exchanges' full read."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def query(self, _sql: str, _params: dict) -> list[dict]:
        return [dict(r) for r in self.rows]


# TEST-WHY: pure-logic-edge — the light read (SQL hashes, no text) and the full read must build identical hash lines and the same in-flight drop, or every stored toc flaps drop→relabel on each pass.
async def test_light_and_full_reads_group_into_the_same_hash_lines():
    import hashlib

    from jobs.tasks.chat_topics import chat_exchanges, group_exchanges

    rows = [
        {"id": "u1", "role": "user", "content": "помидоры", "created_at": "2026-11-01T10:00:00Z"},
        {"id": "a1", "role": "assistant", "content": "", "model": "m", "created_at": "2026-11-01T10:00:01Z"},
        {"id": "a1b", "role": "assistant", "content": "поливать", "model": "m2", "created_at": "2026-11-01T10:00:02Z"},
        {"id": "u2", "role": "user", "content": "цепь", "created_at": "2026-11-01T10:01:00Z"},
        {"id": "a2", "role": "assistant", "content": "  \n ", "created_at": "2026-11-01T10:01:01Z"},
    ]
    # what _LIGHT_SQL returns: crypto::sha256(content), string::trim(content) != ''
    light = [{**{k: v for k, v in r.items() if k != "content"},
              "h": hashlib.sha256(r["content"].encode()).hexdigest(),
              "has": r["content"].strip() != ""} for r in rows]
    grouped, model = group_exchanges(light)
    full = await chat_exchanges(_RowsDB(rows), "c1")
    # u2's answer is whitespace only — the turn in flight, dropped by both reads
    assert [e["id"] for e in grouped] == [e["id"] for e in full] == ["u1"]
    assert model == "m2" and full[0]["asst"] == "поливать"
    assert grouped[0]["hash"] == full[0]["hash"]
    edited = [dict(r, content="полив") if r["id"] == "a1b" else r for r in rows]
    assert (await chat_exchanges(_RowsDB(edited), "c1"))[0]["hash"] != full[0]["hash"]


def _toc(topics: dict[str, list[str]], hashes: dict[str, str], frozen: list[str]) -> dict:
    ids = [m for ms in topics.values() for m in ms]
    return {"topics": [{"title": t, "synopsis": t, "message_ids": ms, "frozen": False}
                       for t, ms in topics.items()],
            "labelled_ids": ids, "labelled_hashes": [hashes[m] for m in ids],
            "frozen_ids": frozen, "model": "m"}


# TEST-WHY: pure-logic-edge — INVARIANT(security) in verify_tocs: an edit drops exactly the topics covering the edited exchange (its synopsis restates edited-away text) and keeps the others with their labelled/frozen ids aligned.
def test_prune_toc_drops_only_topics_covering_a_changed_exchange():
    from jobs.tasks.chat_topics import prune_toc

    toc = _toc({"A": ["u1", "u2"], "B": ["u3"]}, {"u1": "h1", "u2": "h2", "u3": "h3"}, ["u1", "u2"])
    same = [{"id": "u1", "hash": "h1"}, {"id": "u2", "hash": "h2"}, {"id": "u3", "hash": "h3"}]
    assert prune_toc(toc, same) is toc
    edited = [dict(e, hash="x") if e["id"] == "u2" else e for e in same]
    pruned = prune_toc(toc, edited)
    assert [t["title"] for t in pruned["topics"]] == ["B"]
    assert (pruned["labelled_ids"], pruned["labelled_hashes"], pruned["frozen_ids"]) == (["u3"], ["h3"], [])
    assert prune_toc(toc, [e for e in same if e["id"] != "u3"])["labelled_ids"] == ["u1", "u2"]
    assert prune_toc(toc, [dict(e, hash="x") for e in same]) is None
    # a row without aligned hashes (unconverted) keeps nothing
    assert prune_toc({**toc, "labelled_hashes": None}, same) is None


# TEST-WHY: pure-logic-edge — INVARIANT(security) in verify_tocs: a branch seeded from its source must not inherit a topic over an exchange whose text the source edited before its own verify ran; mapping is by origin and the source's STORED hash.
def test_seed_toc_maps_by_origin_and_refuses_an_unverified_source_edit():
    from jobs.tasks.chat_topics_seed import seed_toc

    source = _toc({"A": ["s1"], "B": ["s2"]}, {"s1": "h1", "s2": "h2"}, ["s1"])
    origins = {"s1": "o1", "s2": "o2"}
    branch = [{"id": "b1", "hash": "h1", "user": {"id": "b1", "origin_id": "o1"}},
              {"id": "b2", "hash": "h2", "user": {"id": "b2", "origin_id": "o2"}}]
    seeded = seed_toc(source, origins, branch)
    assert [(t["title"], t["message_ids"]) for t in seeded["topics"]] == [("A", ["b1"]), ("B", ["b2"])]
    assert (seeded["labelled_ids"], seeded["labelled_hashes"], seeded["frozen_ids"]) \
        == (["b1", "b2"], ["h1", "h2"], ["b1"])
    # the source edited s2 after labelling it: its copy's text differs from the stored hash
    edited = [branch[0], dict(branch[1], hash="edited")]
    assert [t["title"] for t in seed_toc(source, origins, edited)["topics"]] == ["A"]
    assert seed_toc(source, origins, [dict(e, hash="x") for e in branch]) is None


# TEST-WHY: cross-process-contract — the worker's labeller and the turn's memory block pick a thread's branch through this one function; newest leaf wins, a tie goes to the later-created branch, a branch with no message is never chosen.
def test_newest_branch_per_thread_picks_newest_leaf_then_newer_branch():
    from datetime import datetime, timezone

    from chat_sessions.conversation import newest_branch_per_thread

    def at(h: int) -> datetime:
        return datetime(2026, 11, 16, h, tzinfo=timezone.utc)

    rows = [
        {"sid": "a", "root": "a", "created_at": at(1)},
        {"sid": "b", "root": "a", "created_at": "2026-11-16T02:00:00Z"},
        {"sid": "c", "root": "a", "created_at": at(3)},  # no messages
        {"sid": "x", "root": "x", "created_at": at(1)},
        {"sid": "y", "root": "x", "created_at": at(2)},
        {"sid": "z", "root": "z", "created_at": at(1)},  # thread without messages
    ]
    last_at = {"a": at(9), "b": at(8), "x": at(5), "y": at(5)}
    picked = {r["root"]: r["sid"] for r in newest_branch_per_thread(rows, last_at)}
    assert picked == {"a": "a", "x": "y"}
