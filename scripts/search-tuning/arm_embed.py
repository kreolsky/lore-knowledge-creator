"""Embed a whole project with the model currently configured. Runs in the backend container."""
import asyncio, sys, time
sys.path.insert(0, "/app")
import config
from db import get_db
from embeddings import _reembed

PROJECT = sys.argv[1]
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 6

async def main():
    print(f"model={config.EMBEDDING_MODEL} min_score={config.RETRIEVAL_MIN_SCORE} "
          f"drop_off={config.RETRIEVAL_SCORE_DROP_OFF}", flush=True)
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, is_reference FROM documents "
        "WHERE project_id = $p AND deleted_at IS NONE AND content != NONE "
        "AND string::len(string::trim(content)) > 0 ",
        # No media_type filter: audio references carry their TRANSCRIPT as content and
        # chunk normally, so the measurement corpus holds what a user actually searches.
        {"p": PROJECT},
    )
    rows = rows or []
    print(f"documents to embed: {len(rows)}", flush=True)
    sem = asyncio.Semaphore(CONC)
    done = {"n": 0, "err": 0}
    t0 = time.time()
    async def one(r):
        async with sem:
            try:
                await _reembed("ref" if r.get("is_reference") else "doc", r["id"], PROJECT)
            except Exception as e:
                done["err"] += 1
                print(f"  ERR {r['id']}: {type(e).__name__}: {e}", flush=True)
            done["n"] += 1
            if done["n"] % 25 == 0:
                print(f"  {done['n']}/{len(rows)}  {time.time()-t0:.0f}s", flush=True)
    await asyncio.gather(*(one(r) for r in rows))
    dt = time.time() - t0
    c = await db.query("SELECT count() FROM doc_chunks WHERE project_id=$p GROUP ALL", {"p": PROJECT})
    st = await db.query(
        "SELECT embedding_status, count() FROM documents WHERE project_id=$p AND deleted_at IS NONE "
        "GROUP BY embedding_status", {"p": PROJECT})
    # The stored vector's length and model stamp are the proof the arm switched.
    v = await db.query(
        "SELECT array::len(embedding) AS dim, model FROM doc_chunks WHERE project_id=$p LIMIT 1",
        {"p": PROJECT})
    dim, model = (v[0].get("dim"), v[0].get("model")) if v else (None, None)
    print(f"done in {dt:.0f}s  errors={done['err']}  chunks={c}  dim={dim}  stored_model={model}")
    print(f"status: {st}")
asyncio.run(main())
