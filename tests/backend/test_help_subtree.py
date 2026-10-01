"""Help subtree — the Lore user guide seeded as a top-level subtree into every project.

What a project SERVES is asserted, not what the repo ships: the stored rows are read
back from the DB after the real seed/sync path ran (lesson
2026-08-05-seeded-content-needs-a-propagation-path).
"""

import re

import pytest
from help_subtree import (
    HELP_DIR,
    HelpBundle,
    HelpPage,
    help_doc_id,
    help_image_id,
    load_bundle,
    page_update,
    render_links,
    sweep_help_subtrees,
    sync_project_help,
)

from config import STORAGE_PATH
from db import create_record, get_db

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _bundle(pages: list[tuple[str, str, str]], images: dict | None = None) -> HelpBundle:
    items = [HelpPage(slug=s, title=t, body=b) for s, t, b in pages]
    return HelpBundle(pages=items, images=images or {})


async def _image_row(pid: str, name: str) -> dict | None:
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, parent_id, is_reference, media_type, file_path, title, "
        "deleted_at FROM type::record('documents', $id)",
        {"id": help_image_id(pid, name)},
    )
    return rows[0] if rows else None


_V1 = [
    ("index", "Guide", "Root. See [Editor](help:editor)."),
    ("editor", "Editor", "Editor v1. Back to [root](help:index)."),
]


async def _rows(pid: str, slugs: list[str]) -> dict[str, dict]:
    db = await get_db()
    out = {}
    for slug in slugs:
        rows = await db.query(
            "SELECT meta::id(id) AS id, project_id, parent_id, title, content, deleted_at, "
            "is_system FROM type::record('documents', $id)",
            {"id": help_doc_id(pid, slug)},
        )
        if rows:
            out[slug] = rows[0]
    return out


# ─── The shipped bundle itself ─────────────────────────────────────────────────


def test_every_help_link_names_a_shipped_page():
    bundle = load_bundle()
    assert bundle.pages[0].slug == "index"
    slugs = {p.slug for p in bundle.pages}
    for page in bundle.pages:
        for target in re.findall(r"\(help:([a-z0-9-]+)\)", page.body):
            assert target in slugs, f"{page.slug} links to unknown help page {target}"


# ─── Replacing a stored page (pure) ────────────────────────────────────────────


def test_page_update_replaces_any_difference():
    pid = "p1"
    slugs = ["index", "editor"]
    old, new = "Old. See [root](help:index).", "New. See [root](help:index)."
    rendered_new = render_links(new, pid, slugs)
    assert rendered_new != new, "links must be rendered to ids in a stored body"

    # an older shipped body AND a user's own text are both replaced
    assert page_update(pid, render_links(old, pid, slugs), new, slugs) == rendered_new
    assert page_update(pid, "My own notes.", new, slugs) == rendered_new
    # already current (a trailing newline is not a difference) → nothing to do
    assert page_update(pid, rendered_new, new, slugs) is None
    assert page_update(pid, rendered_new + "\n", new, slugs) is None


# ─── A new project serves the whole guide ──────────────────────────────────────


@pytest.mark.asyncio
async def test_new_project_serves_the_guide(client, admin_user, enqueue_recorder):
    _uid, token = admin_user
    resp = await client.post("/api/projects", json={"name": "Help Test"},
                             cookies={"lore_session": token})
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json()["project_id"]
    # The create hands the guide to a worker job; run exactly what it posted.
    posted = enqueue_recorder.of("help_seed_task")
    assert [c.args for c in posted] == [(pid,)]
    from jobs.tasks import help_seed_task
    await help_seed_task({}, *posted[0].args)
    bundle = load_bundle()
    slugs = [p.slug for p in bundle.pages]
    rows = await _rows(pid, slugs)
    assert set(rows) == set(slugs)

    root = rows["index"]
    assert root["parent_id"] is None
    assert root["title"] == bundle.pages[0].title
    assert root["is_system"] is not True, "help pages are plain, user-deletable docs"
    live_ids = {r["id"] for r in rows.values() if r["deleted_at"] is None}
    for slug in slugs[1:]:
        assert rows[slug]["parent_id"] == root["id"]
    for slug, row in rows.items():
        assert "help:" not in row["content"], f"{slug}: unrendered help link"
        assert "help-image:" not in row["content"], f"{slug}: unrendered help picture"
        for target in _LINK.findall(row["content"]):
            if target.startswith("sys-help-"):
                assert target in live_ids, f"{slug}: link to {target} does not resolve"

    # The guide sits BELOW the project's own documents at the top level.
    db = await get_db()
    top = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid AND parent_id IS NONE "
        "AND deleted_at IS NONE AND is_reference != true ORDER BY sort_key ASC",
        {"pid": pid},
    )
    assert top[-1]["id"] == root["id"]

    # Every shipped picture is an image reference of the page that shows it, with its
    # file in storage, and the page's embed points at it.
    assert bundle.images, "the guide ships pictures"
    for name in bundle.images:
        host = next(s for s, r in rows.items() if f"(ref:{help_image_id(pid, name)})" in r["content"])
        ref = await _image_row(pid, name)
        assert ref and ref["is_reference"] is True and ref["media_type"] == "image"
        assert ref["parent_id"] == rows[host]["id"]
        assert (STORAGE_PATH / ref["file_path"]).is_file()


# ─── Propagation over an existing project ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_replaces_every_page_including_edited(test_db, project_with_doc):
    pid, _idx, _ = project_with_doc
    await sync_project_help(pid, _bundle(_V1))

    # The user edits and renames the root; the editor page stays as shipped.
    db = await get_db()
    await db.query("UPDATE type::record('documents', $id) SET content = 'My own root text.', title = 'Mine'",
                   {"id": help_doc_id(pid, "index")})

    v2_pages = [("index", "Guide", "Root v2."), ("editor", "Editor", "Editor v2. Back to [root](help:index).")]
    await sync_project_help(pid, _bundle(v2_pages))

    rows = await _rows(pid, ["index", "editor"])
    assert rows["index"]["content"] == "Root v2.", "an update replaces a user-edited page"
    assert rows["index"]["title"] == "Guide", "an update restores the shipped title"
    assert rows["editor"]["content"] == render_links(v2_pages[1][2], pid, ["index", "editor"])


@pytest.mark.asyncio
async def test_sync_respects_deletion_and_moves(test_db, project_with_doc):
    pid, _idx, admin = project_with_doc
    await sync_project_help(pid, _bundle(_V1))
    db = await get_db()
    # The user deletes the editor page and moves the root to another project.
    await db.query("UPDATE type::record('documents', $id) SET deleted_at = time::now()",
                   {"id": help_doc_id(pid, "editor")})
    await create_record("projects", "help-other-project", {
        "name": "Other", "status": "active", "project_context": "",
        "index_doc_id": "help-other-idx", "owner_id": admin,
    })
    await db.query("UPDATE type::record('documents', $id) SET project_id = 'help-other-project'",
                   {"id": help_doc_id(pid, "index")})

    changed = [("index", "Guide", "Root v2."), ("editor", "Editor", "Editor v2.")]
    await sync_project_help(pid, _bundle(changed))

    rows = await _rows(pid, ["index", "editor"])
    assert rows["editor"]["deleted_at"] is not None, "a deleted page is never resurrected"
    assert rows["editor"]["content"] == render_links(_V1[1][2], pid, ["index", "editor"])
    assert rows["index"]["project_id"] == "help-other-project", "a moved page is left where the user put it"
    assert rows["index"]["content"] == render_links(_V1[0][2], pid, ["index", "editor"])
    await db.query("DELETE documents WHERE project_id = 'help-other-project'; "
                   "DELETE type::record('projects', 'help-other-project')")


@pytest.mark.asyncio
async def test_sync_adds_a_newly_shipped_page_only_under_a_live_root(test_db, project_with_doc):
    pid, _idx, _ = project_with_doc
    await sync_project_help(pid, _bundle(_V1))
    grown = _V1 + [("search", "Search", "Search page.")]
    await sync_project_help(pid, _bundle(grown))
    rows = await _rows(pid, ["search"])
    assert rows["search"]["parent_id"] == help_doc_id(pid, "index")

    db = await get_db()
    await db.query("UPDATE type::record('documents', $id) SET deleted_at = time::now()",
                   {"id": help_doc_id(pid, "index")})
    await sync_project_help(pid, _bundle(grown + [("voice", "Voice", "Voice page.")]))
    assert await _rows(pid, ["voice"]) == {}, "no page is created once the user deleted the guide"


@pytest.mark.asyncio
async def test_sweep_runs_once_per_bundle(test_db, project_with_doc):
    pid, _idx, _ = project_with_doc
    db = await get_db()
    await db.query("DELETE app_meta:help_bundle")
    bundle = _bundle(_V1)
    assert await sweep_help_subtrees(bundle) >= 1
    assert set(await _rows(pid, ["index", "editor"])) == {"index", "editor"}

    # Same bundle again → the sweep is a no-op, even for a project that lost a page.
    await db.query("DELETE type::record('documents', $id)", {"id": help_doc_id(pid, "editor")})
    assert await sweep_help_subtrees(bundle) == 0
    assert set(await _rows(pid, ["index", "editor"])) == {"index"}
    await db.query("DELETE app_meta:help_bundle")


# ─── The agent is pointed at the guide, never fed its body ─────────────────────


def test_prompt_help_section_only_with_a_root():
    from agent_config import build_agent_system_prompt
    with_root = build_agent_system_prompt({}, help_root={"id": "sys-help-p-index", "title": "Справка Lore"})
    assert "# Lore help" in with_root
    assert "[Справка Lore](sys-help-p-index)" in with_root
    assert "# Lore help" not in build_agent_system_prompt({})


@pytest.mark.asyncio
async def test_turn_prompt_points_at_the_live_root_only(test_db, project_with_doc):
    from agent_config import build_prompt_and_skill_docs
    pid, _idx, admin = project_with_doc
    bundle = _bundle([("index", "Guide", "ROOT-BODY-MARKER"), ("editor", "Editor", "EDITOR-BODY-MARKER")])
    await sync_project_help(pid, bundle)
    root_id = help_doc_id(pid, "index")

    prompt, _ = await build_prompt_and_skill_docs(pid)
    assert f"[Guide]({root_id})" in prompt
    assert "ROOT-BODY-MARKER" not in prompt and "EDITOR-BODY-MARKER" not in prompt

    # Moved to another project → no pointer here.
    db = await get_db()
    await create_record("projects", "help-prompt-other", {
        "name": "Other", "status": "active", "project_context": "",
        "index_doc_id": "help-prompt-other-idx", "owner_id": admin,
    })
    await db.query("UPDATE type::record('documents', $id) SET project_id = 'help-prompt-other'", {"id": root_id})
    prompt, _ = await build_prompt_and_skill_docs(pid)
    assert "# Lore help" not in prompt

    # Back home but deleted → no pointer.
    await db.query("UPDATE type::record('documents', $id) SET project_id = $pid, deleted_at = time::now()",
                   {"id": root_id, "pid": pid})
    prompt, _ = await build_prompt_and_skill_docs(pid)
    assert "# Lore help" not in prompt
    await db.query("DELETE type::record('projects', 'help-prompt-other')")


@pytest.mark.asyncio
async def test_sync_seeds_a_picture_once_and_only_where_shown(test_db, project_with_doc):
    pid, _idx, _ = project_with_doc
    pic = {"pic": HELP_DIR / "images" / "wet-cat.jpg"}
    pages = [("index", "Guide", "Root."), ("editor", "Editor", "Look: ![Cat|300](help-image:pic)")]
    await sync_project_help(pid, _bundle(pages, images=pic))

    ref = await _image_row(pid, "pic")
    assert ref["parent_id"] == help_doc_id(pid, "editor")
    assert ref["title"] == "Cat"
    rows = await _rows(pid, ["editor"])
    assert rows["editor"]["content"] == f"Look: ![Cat|300](ref:{help_image_id(pid, 'pic')})"

    # The user deletes the picture: a later sync must not bring it back.
    db = await get_db()
    await db.query("UPDATE type::record('documents', $id) SET deleted_at = time::now()",
                   {"id": help_image_id(pid, "pic")})
    await sync_project_help(pid, _bundle(pages, images=pic))
    assert (await _image_row(pid, "pic"))["deleted_at"] is not None


@pytest.mark.asyncio
async def test_sync_gives_an_edited_page_its_new_picture(test_db, project_with_doc):
    pid, _idx, _ = project_with_doc
    await sync_project_help(pid, _bundle(_V1))
    db = await get_db()
    await db.query("UPDATE type::record('documents', $id) SET content = 'Mine, no pictures.'",
                   {"id": help_doc_id(pid, "editor")})
    shipped = [_V1[0], ("editor", "Editor", "Editor v2 ![Cat](help-image:pic)")]
    await sync_project_help(pid, _bundle(shipped, {"pic": HELP_DIR / "images" / "wet-cat.jpg"}))
    ref = await _image_row(pid, "pic")
    assert ref and ref["parent_id"] == help_doc_id(pid, "editor"), "the replaced page gets its picture"


@pytest.mark.asyncio
async def test_sync_restores_the_shipped_page_order(test_db, project_with_doc):
    pid, _idx, _ = project_with_doc
    pages = [_V1[0], ("a", "A", "a"), ("b", "B", "b"), ("c", "C", "c")]
    await sync_project_help(pid, _bundle(pages))
    from documents.service import sibling_rows
    from documents.update import reorder_document_command

    # The user shuffles the pages; the next update puts them back in file order.
    await reorder_document_command(help_doc_id(pid, "c"), None)
    await reorder_document_command(help_doc_id(pid, "a"), help_doc_id(pid, "b"))
    order = lambda rows: [r["id"] for r in rows]  # noqa: E731
    assert order(await sibling_rows(pid, help_doc_id(pid, "index"))) != [help_doc_id(pid, s) for s in "abc"]

    await sync_project_help(pid, _bundle(pages))
    assert order(await sibling_rows(pid, help_doc_id(pid, "index"))) == [help_doc_id(pid, s) for s in "abc"]


@pytest.mark.asyncio
async def test_sweep_survives_one_broken_project(test_db, project_with_doc):
    pid, _idx, admin = project_with_doc
    db = await get_db()
    await db.query("DELETE app_meta:help_bundle")
    # A project whose guide cannot be created: its path is already taken there.
    broken = "help-broken-project"
    await create_record("projects", broken, {
        "name": "Broken", "status": "active", "project_context": "",
        "index_doc_id": "help-broken-idx", "owner_id": admin,
    })
    await create_record("documents", "help-broken-squatter", {
        "project_id": broken, "title": "Squatter", "content": "",
        "path": f".lore/help/{help_doc_id(broken, 'index')}",
    })

    await sweep_help_subtrees(_bundle(_V1))
    assert set(await _rows(pid, ["index", "editor"])) == {"index", "editor"}, "other projects still sync"
    assert not await _rows(broken, ["index"]), "the broken project really failed"
    assert not await db.query("SELECT digest FROM app_meta:help_bundle"), "a partial sweep is retried next boot"
    await db.query("DELETE app_meta:help_bundle; DELETE documents WHERE project_id = $b; "
                   "DELETE type::record('projects', $b)", {"b": broken})


# ─── The guide is never embedded ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guide_is_not_an_embedding_candidate(test_db, project_with_doc):
    pid, idx, _ = project_with_doc
    await sync_project_help(pid, _bundle(_V1))
    from embedding_coverage import chunkable_candidates
    db = await get_db()
    ids = {c["id"] for c in await chunkable_candidates(db, pid)}
    assert not {i for i in ids if i.startswith("sys-help-")}, "guide pages must not be embedding candidates"


@pytest.mark.asyncio
async def test_saving_a_guide_page_queues_no_embed(test_db, project_with_doc, enqueue_recorder):
    pid, idx, _ = project_with_doc
    from embeddings import _on_content_flushed
    await _on_content_flushed("doc", help_doc_id(pid, "editor"), pid)
    assert enqueue_recorder.of("embed_document_task") == []
    # The refused case has a passing twin: an ordinary document IS queued.
    await _on_content_flushed("doc", idx, pid)
    assert [c.args[1] for c in enqueue_recorder.of("embed_document_task")] == [idx]
