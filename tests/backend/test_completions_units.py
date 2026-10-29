"""Unit tests for completions' decomposed turn helpers (R1 chat round).

`create_completion` was a 307-line route. The extracted turn phases are pinned
here at the unit level: session guards, the driver-owned send's failure ladder
+ followup payload (run_harness_turn), and the claim-time row mint
(driver.persistence._mint_claim_rows). Live coverage is test_harness_turn's.
"""

from uuid import uuid4

# ─── _mint_claim_rows (the pair, written at dsh's claim) ─────────────────────
# Against the REAL test DB: the head lookup and the tail read are queries whose
# semantics (ORDER BY, deleted_at, type::record ids) a fake would only mirror.


async def _chat(db, cid: str, **fields) -> None:
    await db.query(
        "CREATE type::record('chat_sessions', $id) CONTENT $f",
        {"id": cid, "f": {"project_id": "p1", "user_id": "u1", "title": "t", **fields}},
    )


async def _rows(db, cid: str) -> list[dict]:
    return await db.query(
        "SELECT meta::id(id) AS mid, parent_id, role, content, model, images "
        "FROM messages WHERE chat_id = $cid ORDER BY created_at",
        {"cid": cid},
    ) or []


def _claim(**kw) -> dict:
    return {"user_msg_id": f"um-{uuid4()}", "assistant_msg_id": f"am-{uuid4()}",
            "user_text": "hello", "images": [], "model": "m1", **kw}


class TestMintClaimRows:


    async def test_a_live_continuation_is_the_head(self, test_db):
        """A message queued before a compaction answers in the continuation;
        a deleted continuation is never the head."""
        from driver.persistence import _mint_claim_rows

        src = f"chat-{uuid4()}"
        await _chat(test_db, src)
        cont, dead = f"cont-{uuid4()}", f"dead-{uuid4()}"
        await _chat(test_db, cont, compacted_from=src)
        await _chat(test_db, dead, compacted_from=src)
        await test_db.query(
            "UPDATE type::record('chat_sessions', $id) SET deleted_at = time::now()",
            {"id": dead})

        head = await _mint_claim_rows(src, _claim())

        assert head["cid"] == cont
        assert len(await _rows(test_db, cont)) == 2
        assert await _rows(test_db, src) == []

    async def test_no_live_chat_mints_nothing(self, test_db):
        from driver.persistence import _mint_claim_rows

        claim = _claim()
        assert await _mint_claim_rows(f"gone-{uuid4()}", claim) is None
        assert await test_db.query(
            "SELECT * FROM type::record('messages', $id)",
            {"id": claim["user_msg_id"]}) == []
