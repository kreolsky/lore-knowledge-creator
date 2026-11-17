"""conversation memory — the block's budget rule, pure (no DB)."""

from chat_memory import HEADER, render_block

from config import count_tokens_approx


def _chat(n: int, topics: int) -> dict:
    return {"sid": f"c{n}", "title": f"Chat {n}", "date": "2026-11-14",
            "topics": [{"title": f"T{n}.{i}", "synopsis": "s" * 120, "message_ids": [f"m{n}{i}"]}
                       for i in range(topics)]}


# TEST-WHY: pure-logic-edge — chats go in whole and in order until the one that crosses the budget (included), the first chat always fits however big, no title-only line is ever emitted, and the shown list (the chat's memory record) is exactly the chats in the text.
def test_whole_chats_until_the_one_crossing_the_budget():
    chats = [_chat(0, 40)] + [_chat(n, 1) for n in range(1, 60)]
    block, shown = render_block(chats, 2500)
    entries = block.split("\n\n")
    assert entries[0] == HEADER
    entries = entries[1:]
    assert all("\n- " in e for e in entries)  # every chat with its topic lines
    assert [e.split("(chat ")[1].split(")")[0] for e in entries] == [f"c{n}" for n in range(len(entries))]
    assert [c["sid"] for c in shown] == [f"c{n}" for n in range(len(entries))]
    assert entries[0].count("\n- ") == 40
    assert len(entries) < len(chats)
    cost = [count_tokens_approx("\n\n" + e) for e in entries]
    head = count_tokens_approx(HEADER)
    assert head + sum(cost[:-1]) < 2500 <= head + sum(cost)  # stops right after the crossing chat
    assert render_block([_chat(0, 40)] + chats[1:], 10)[0].count("(chat ") == 1
    assert render_block([], 8000) == ("", [])
