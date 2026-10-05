"""Create (or reset) the synthetic RU/EN cross-language bench project on this DB."""
import asyncio, json, sys, uuid
from datetime import datetime, timezone
sys.path.insert(0, "/app")
from db import get_db
PID = "e11c0000-0000-4000-8000-000000000001"
NAME = "XLing Bench"
async def main():
    db = await get_db()
    data = json.load(open("/tmp/xling_data.json", encoding="utf-8"))
    owner = (await db.query("SELECT meta::id(id) AS id FROM users WHERE username='admin' LIMIT 1")
             or await db.query("SELECT meta::id(id) AS id FROM users LIMIT 1"))[0]["id"]
    await db.query("DELETE doc_chunks WHERE project_id=$p", {"p": PID})
    await db.query("DELETE documents WHERE project_id=$p", {"p": PID})
    await db.query("DELETE projects WHERE meta::id(id)=$p", {"p": PID})
    now = datetime.now(timezone.utc)
    idx = str(uuid.uuid4())
    await db.query(
        "CREATE type::record('projects',$p) SET name=$n, owner_id=$o, status='active', "
        "is_public=false, project_context='', index_doc_id=$i, created_at=$t",
        {"p": PID, "n": NAME, "o": owner, "i": idx, "t": now})
    async def mkdoc(did, title, content, path, is_index, sort):
        await db.query(
            "CREATE type::record('documents',$id) SET project_id=$p, title=$t, content=$c, "
            "path=$path, parent_id=$par, sort_key=$s, content_version=1, archived=false, "
            "is_index=$ix, is_memory=false, is_reference=false, is_system=false, "
            "embedding_status='ok', last_editor_id=$o, last_editor_name='admin', "
            "created_at=$now, updated_at=$now",
            {"id": did, "p": PID, "t": title, "c": content, "path": path,
             "par": None if is_index else idx, "s": sort, "ix": is_index, "o": owner, "now": now})
    await mkdoc(idx, NAME, "Synthetic cross-language retrieval bench.", "index.md", True, "A0")
    for i, d in enumerate(data["docs"]):
        await mkdoc(str(uuid.uuid4()), d["t"], d["c"], f"{i:02d}-{d['l']}.md", False, f"B{i:02d}")
    n = await db.query("SELECT count() FROM documents WHERE project_id=$p GROUP ALL", {"p": PID})
    print(f"project {PID} seeded: {n}")
asyncio.run(main())
