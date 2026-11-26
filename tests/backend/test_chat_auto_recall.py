"""persona auto-recall — the gate, the block's shape and budget, and the wall the quoted
originals pass through (chat_auto_recall.py). The DB test drives the REFUSED rows: a
second member's chat, another persona's chat and a deleted message are all indexed and
still in the tables — only the read-time predicate stands between them and the block.
"""
import pytest
from chat_auto_recall import (
    ANSWER_CHARS,
    GATE_MIN_WORDS,
    HEADER,
    USER_CHARS,
    build_recall_block,
    gate_reason,
    group_exchanges,
    render_recall,
)
from helpers import fake_embedding_provider

from config import count_tokens_approx
from jobs.tasks.chat_index import index_chat_messages

_AXES = ("dragon", "lighthouse", "comet", "harbor", "ember", "glacier", "orchard")


def _vec(text: str) -> list[float]:
    low = text.lower()
    return [1.0 if a in low else 0.0 for a in _AXES] + [0.01]


def _exchange(sid: str, user: str, score: float, answer: str | None = "an answer") -> dict:
    return {"score": score,
            "user": {"id": user, "chat_id": sid, "content": f"words of {user}", "created_at": None},
            "answer": {"id": f"{user}-a", "content": answer} if answer else None}


def _sessions(*sids: str, topics: dict | None = None) -> dict:
    return {sid: {"sid": sid, "title": f"Chat {sid}", "topics": (topics or {}).get(sid, [])}
            for sid in sids}


# TEST-WHY: pure-logic-edge — the gate counts words not punctuation, groups cap at 5 × 2 best-first under the topic that holds the exchange, the budget stops right after the crossing exchange and never emits a bare header, and a quote is clipped 500/250.
def test_gate_groups_budget_and_clipping():
    assert gate_reason("привет") == f"1 words, fewer than {GATE_MIN_WORDS}"
    assert gate_reason("спасибо, ок") is not None
    assert gate_reason("ок, давай!!") is not None
    assert gate_reason("…—?!") is not None
    assert gate_reason("") is not None
    assert gate_reason("Привет, как у тебя дела?") is None  # 4 words
    assert gate_reason("что-то не так с договором") is None  # a hyphenated word is one word

    topic = {"title": "Dragon · sleep", "synopsis": "where the dragon sleeps · at night",
             "message_ids": ["u2"]}
    whole = {"title": "Chat A", "synopsis": "all of chat A", "message_ids": ["u1", "u3"]}
    sessions = _sessions("a", "a2", "b", "c", "d", "e", "f", topics={"a": [topic, whole], "a2": [topic]})
    # Chat `a`: two topics = two groups (u2 alone, then u1 + u3); `a2` shares a topic
    # (a third group); b..f are one exchange each — 8 groups, the best 5 survive.
    exchanges = [
        _exchange("a", "u1", 0.90), _exchange("a", "u2", 0.95), _exchange("a", "u3", 0.70),
        _exchange("a2", "u2b", 0.60), _exchange("b", "b1", 0.85), _exchange("c", "c1", 0.80),
        _exchange("d", "d1", 0.75), _exchange("e", "e1", 0.65), _exchange("f", "f1", 0.50),
    ]
    groups = group_exchanges(sessions, exchanges)
    assert [(g["sid"], [e["user"]["id"] for e in g["exchanges"]]) for g in groups] == [
        ("a", ["u2"]), ("a", ["u1", "u3"]), ("b", ["b1"]), ("c", ["c1"]), ("d", ["d1"])]
    assert [g["topic"] is None for g in groups] == [False, False, True, True, True]
    assert groups[1]["topic"] is whole  # under the topic that holds the exchange, in the labeller's order

    long_user, long_answer = "u" * (USER_CHARS + 40), "a" * (ANSWER_CHARS + 40)
    big = [{"sid": "a", "title": "Chat a", "date": "2026-11-14", "topic": topic,
            "exchanges": [_exchange("a", "u1", 0.9, long_answer)
                          | {"user": {"id": "u1", "chat_id": "a", "content": long_user,
                                      "created_at": None}},
                          _exchange("a", "u2", 0.8, None)]}]
    block, chats, rendered, tokens = render_recall(big, 10_000)
    head, first, second = block.split("\n\n")
    assert head == HEADER and head.startswith("# Earlier conversations")
    assert first.startswith("2026-11-14 · Chat a (chat a) — topic: Dragon - sleep — "
                            "where the dragon sleeps - at night\n")
    user_line, answer_line = first.split("\n")[1:]
    assert user_line == f"user (message u1) : {long_user[:USER_CHARS]} …"
    assert answer_line == f"assistant (message u1-a): {long_answer[:ANSWER_CHARS]} …"
    assert second == "user (message u2) : words of u2"  # no answer, no second header
    assert (chats, rendered) == ([{"sid": "a", "date": "2026-11-14"}], 1)  # one group, one chat
    assert 0 < tokens <= count_tokens_approx(block) and tokens >= count_tokens_approx(block) - len(block.split("\n\n"))

    # The budget: everything up to the exchange that crosses it, nothing after; the
    # first exchange goes in whatever the budget says.
    many = [{"sid": s, "title": f"Chat {s}", "date": "2026-11-14", "topic": None,
             "exchanges": [_exchange(s, f"{s}u", 0.5)]} for s in "abcde"]
    full, chats, rendered, tokens = render_recall(many, 10_000)
    assert ([c["sid"] for c in chats], rendered, full.count("\n\n")) == (list("abcde"), 5, 5)
    cut, chats, rendered, _ = render_recall(many, tokens - 10)
    assert ([c["sid"] for c in chats], rendered) == (list("abcd"), 4) and cut == full.rsplit("\n\n", 1)[0]
    first_only, chats, rendered, _ = render_recall(many, 1)
    assert first_only.count("\n\n") == 1 and chats == [{"sid": "a", "date": "2026-11-14"}]
    assert render_recall([], 2000) == ("", [], 0, 0)


async def _session(db, sid, uid, pid, persona, **fields):
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": f"title {sid}",
                          "model": "m", "origin_persona_id": persona, **fields}},
    )


async def _msg(db, mid, chat_id, content, *, role="user", parent_id=None):
    await db.query(
        "CREATE type::record('messages', $id) CONTENT $f",
        {"id": mid, "f": {"chat_id": chat_id, "role": role, "content": content,
                          "parent_id": parent_id}},
    )


@pytest.fixture
async def auto_floor(test_db):
    """The auto-recall floor as an admin override row — a settings override, the way
    an operator raises it; the override cache is dropped before and after."""
    import json

    import settings

    key = "CHAT_RECALL_AUTO_MIN_SCORE"
    await test_db.query(
        "UPSERT type::record('instance_settings', $k) SET key = $k, value = $v, updated_by = 'test'",
        {"k": key, "v": json.dumps(0.5)})
    settings.drop_cache()
    yield 0.5
    settings.drop_cache()
    await test_db.query("DELETE type::record('instance_settings', $k)", {"k": key})


# TEST-WHY: refused-principal — a second member's chat, another persona's chat, a deleted message and a small-talk exchange are indexed yet denied a place in the block; the gate costs no embedding call, and the quoted answer brings its question.
async def test_block_quotes_only_this_persons_live_chats_of_the_persona(
    test_db, admin_user, regular_user, project_with_doc, embed, auto_floor,
):
    pid, _, a = project_with_doc
    b = regular_user[0]
    await test_db.query(
        "CREATE type::record('project_members', $id) SET project_id = $pid, "
        "user_id = $uid, access_level = 'full'",
        {"id": f"pm-recall-{pid}-{b}", "pid": pid, "uid": b},
    )
    await _session(test_db, "r1", a, pid, "P")
    await _msg(test_db, "r1m0", "r1", "where does the dragon sleep?")
    await _msg(test_db, "r1m1", "r1", "The dragon sleeps under the lighthouse.",
               role="assistant", parent_id="r1m0")
    await _msg(test_db, "r1m2", "r1", "and where is the comet?")
    await _msg(test_db, "r1m3", "r1", "A comet over the harbor.", role="assistant", parent_id="r1m2")
    # A greeting exchange: the twin of any longer greeting, and no subject.
    await _msg(test_db, "r1m4", "r1", "привет")
    await _msg(test_db, "r1m5", "r1", "Привет! Чем помочь?", role="assistant", parent_id="r1m4")
    await test_db.query(
        "CREATE type::record('chat_tocs', 'r1') CONTENT $t",
        {"t": {"user_id": a, "project_id": pid, "chat_id": "r1", "model": "m",
               "frozen_ids": [], "labelled_ids": ["r1m0", "r1m2"], "labelled_hashes": [],
               "topics": [{"title": "Dragon", "synopsis": "the dragon's bed",
                           "message_ids": ["r1m0"], "frozen": False},
                          {"title": "Comet", "synopsis": "the comet",
                           "message_ids": ["r1m2"], "frozen": False}]}},
    )
    await _session(test_db, "r2", a, pid, "P")
    await _msg(test_db, "r2m0", "r2", "the glacier of the north")  # another subject: no hit
    await _session(test_db, "r3", a, pid, "OTHER-PERSONA")
    await _msg(test_db, "r3m0", "r3", "the ember dragon nobody else may see")
    await _session(test_db, "r4", a, pid, "P")
    await _msg(test_db, "r4m0", "r4", "the orchard dragon comet harbor, deleted since")
    await _msg(test_db, "r4m1", "r4", "sure", role="assistant", parent_id="r4m0")
    await _session(test_db, "rb1", b, pid, "P")
    await _msg(test_db, "rb1m0", "rb1", "the harbor comet dragon of another member")
    await _session(test_db, "rfresh", a, pid, "P")
    await index_chat_messages({}, a, pid)
    await index_chat_messages({}, b, pid)
    # Indexed, then deleted: the chunk that would score highest is still in the index.
    await test_db.query("UPDATE type::record('messages', 'r4m0') SET deleted_at = time::now()")
    embed.reset_mock()
    session = (await test_db.query("SELECT * FROM type::record('chat_sessions', 'rfresh')"))[0]

    async def recall(text: str):
        return await build_recall_block(
            test_db, session_id="rfresh", session=session, user_id=a, project_id=pid,
            persona_id="P", text=text, budget=2000)

    gated = await recall("спасибо, ок!")
    assert (gated.block, gated.chats, gated.best_score) == ("", [], None)
    assert embed.await_count == 0, "a gated message must cost no embedding call"

    # The query hits r1's comet ANSWER (0.82) and both questions (0.58): the answer
    # brings its question; r4's indexed-but-deleted chunk (0.87) and rb1's (1.0) stay
    # behind the wall — and neither reads as the best score.
    result = await recall("where does the dragon sleep, and the comet over the harbor?")
    assert 0.8 < result.best_score < 0.83 and result.groups == 2
    assert result.chats == [{"sid": "r1", "date": result.chats[0]["date"]}]
    assert embed.await_count == 1, "one embedding call carries every query piece"
    head, comet, dragon = result.block.split("\n\n")
    assert head == HEADER
    assert comet.startswith(f"{result.chats[0]['date']} · title r1 (chat r1) — topic: Comet — the comet\n")
    assert comet.endswith(f"user (message r1m2) {result.chats[0]['date']}: and where is the comet?\n"
                          "assistant (message r1m3): A comet over the harbor.")
    assert dragon.startswith(f"{result.chats[0]['date']} · title r1 (chat r1) — topic: Dragon — the dragon's bed\n")
    assert dragon.endswith(f"user (message r1m0) {result.chats[0]['date']}: where does the dragon sleep?\n"
                           "assistant (message r1m1): The dragon sleeps under the lighthouse.")
    for denied in ("nobody else may see", "deleted since", "another member",
                   "title r2", "title r3", "title r4", "title rb1", "glacier"):
        assert denied not in result.block, f"{denied!r} reached the block — the wall must deny it"

    # The gate's other side: the greeting exchange scores highest for a longer greeting
    # (identical vectors, 1.0) yet is never quoted and does not read as the best score.
    embed.reset_mock()
    small = await recall("Привет! Как у тебя дела сегодня?")
    assert (small.block, small.chats, small.best_score, small.groups) == ("", [], None, 0)
    assert embed.await_count == 1, "the greeting passes the gate: one embedding call, nothing quoted"


@pytest.fixture
def embed(monkeypatch):
    return fake_embedding_provider(monkeypatch, _vec)
