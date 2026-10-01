"""The Lore user guide, seeded as a top-level subtree into every project.

# SYSTEM: help-subtree — the Lore user guide seeded as a top-level subtree into every project

# ARCH: the guide's source is `backend/configs/help/NN-<slug>.md` (first line
# `# <Title>`, the rest is the body, `00-index` is the root). Every project holds its
# OWN copy as plain documents (is_system=false) so a user can read, edit, move and
# delete it like anything else; the agent is pointed at the root by
# agent_config.build_agent_system_prompt and reads pages on demand — the guide body
# is never injected.

# ARCH: identity is the deterministic id `sys-help-<project_id>-<slug>`, never the
# path or the title — a rename or an in-project move keeps the page recognized.

# ARCH: propagation runs on two paths and nowhere else: `create_project` seeds a new
# project, and the boot sweep (`sweep_help_subtrees`, lifespan) re-syncs every project
# once per shipped bundle (gated on `app_meta:help_bundle.digest`). Both go through
# `sync_project_help`, so a new project and an old one follow one rule.

Cross-page links are written `[text](help:<slug>)` in the files and rendered to
`[text](<doc id>)` when stored. Pictures are written
`![alt|size](help-image:<name>)` and rendered to `(ref:<id>)`: the file
`images/<name>.jpg` is seeded as an image reference of the page that shows it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agent_config_seed import _deterministic_id, help_doc_id
from documents.service import (
    create_document,
    create_reference_row,
    rename_document,
    sibling_rows,
)
from documents.update import reorder_document_command
from PIL import Image
from sort_keys import key_between, n_keys_between

import event_bus
from config import STORAGE_PATH
from db import get_db
from jobs import pool as jobs_pool
from mentions import rebuild_doc_mentions
from ydoc_store import set_content

logger = logging.getLogger(__name__)

HELP_DIR = Path(__file__).parent / "configs" / "help"
_PAGE_FILE = re.compile(r"^\d\d-([a-z0-9-]+)\.md$")
_HELP_LINK = re.compile(r"\(help:([a-z0-9-]+)\)")
_HELP_IMAGE = re.compile(r"\(help-image:([a-z0-9-]+)\)")
_HELP_IMAGE_ALT = re.compile(r"!\[([^\]|]*)(?:\|[^\]]*)?\]\(help-image:([a-z0-9-]+)\)")


@dataclass(frozen=True)
class HelpPage:
    slug: str
    title: str
    body: str


@dataclass(frozen=True)
class HelpBundle:
    """The shipped guide: pages in tree order (root first) and the picture files by name."""
    pages: list[HelpPage]
    images: dict[str, Path] = field(default_factory=dict)

    def digest(self) -> str:
        """Identity of the whole bundle — the boot sweep runs once per value."""
        payload = json.dumps(
            [[p.slug, p.title, p.body] for p in self.pages]
            + [[n, hashlib.sha256(self.images[n].read_bytes()).hexdigest()] for n in sorted(self.images)],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_bundle() -> HelpBundle:
    """Read the guide from backend/configs/help/. Crashes on a malformed page."""
    pages: list[HelpPage] = []
    for path in sorted(HELP_DIR.glob("*.md")):
        match = _PAGE_FILE.match(path.name)
        if not match:
            raise RuntimeError(f"help page file name must be NN-<slug>.md: {path.name}")
        first, _, body = path.read_text(encoding="utf-8").partition("\n")
        if not first.startswith("# "):
            raise RuntimeError(f"help page must start with '# <Title>': {path.name}")
        pages.append(HelpPage(slug=match.group(1), title=first[2:].strip(), body=body.strip()))
    if not pages or pages[0].slug != "index":
        raise RuntimeError("help bundle must start with 00-index.md")
    images = {p.stem: p for p in sorted((HELP_DIR / "images").glob("*.jpg"))}
    return HelpBundle(pages=pages, images=images)


def help_image_id(project_id: str, name: str) -> str:
    return _deterministic_id(project_id, "help", f"-image-{name}")


def render_links(
    body: str, project_id: str, slugs: Iterable[str], images: Iterable[str] = (),
) -> str:
    """`(help:<slug>)` → `(<doc id>)` and `(help-image:<name>)` → `(ref:<id>)` for
    every shipped slug and picture."""
    known, pictures = set(slugs), set(images)
    body = _HELP_LINK.sub(
        lambda m: f"({help_doc_id(project_id, m.group(1))})" if m.group(1) in known else m.group(0),
        body,
    )
    return _HELP_IMAGE.sub(
        lambda m: f"(ref:{help_image_id(project_id, m.group(1))})" if m.group(1) in pictures else m.group(0),
        body,
    )


def page_update(
    project_id: str, stored: str, current_body: str,
    slugs: Iterable[str], images: Iterable[str] = (),
) -> str | None:
    """The rendered body a stored page must be replaced with, or None when it already is.

    # INVARIANT(persisted): an update of the guide replaces every live page of it with
    # the shipped text, whether or not the user edited that page.
    # Why: operator ruling — the guide is Lore's documentation and must match the
    # shipped version in every project; a user's edits to it are not kept across an
    # update. Only deletion and a move to another project are respected (see
    # sync_project_help).
    """
    rendered = render_links(current_body, project_id, slugs, images)
    return None if (stored or "").strip() == rendered.strip() else rendered


async def _stored_pages(db, project_id: str, slugs: list[str]) -> dict[str, dict]:
    """Every row (live or tombstoned, any project) holding a help id of this project."""
    in_clause = ",".join(f"type::record('documents', $i{n})" for n in range(len(slugs)))
    params = {f"i{n}": help_doc_id(project_id, s) for n, s in enumerate(slugs)}
    rows = await db.query(
        "SELECT meta::id(id) AS id, project_id, title, content, deleted_at FROM documents "
        f"WHERE id IN [{in_clause}]",
        params,
    )
    by_id = {r["id"]: r for r in (rows or [])}
    return {s: by_id[help_doc_id(project_id, s)] for s in slugs if help_doc_id(project_id, s) in by_id}


async def _root_sort_key(db, project_id: str) -> str:
    """A key BELOW every live top-level document: the guide sits at the bottom."""
    rows = await db.query(
        "SELECT sort_key FROM documents WHERE project_id = $pid AND parent_id IS NONE "
        "AND deleted_at IS NONE AND is_reference != true AND sort_key IS NOT NONE "
        "ORDER BY sort_key DESC LIMIT 1",
        {"pid": project_id},
    )
    return key_between(rows[0]["sort_key"] if rows else None, None)


async def _refresh(db, doc_id: str, project_id: str, content: str) -> None:
    """Rewrite a stored page through the one canonical content writer."""
    # WHY: set_content(persist=True) is the single writer of content + ydoc_state
    # and fans the snapshot out to any live editor session (see INVARIANT(corruption)
    # in documents/update.py) — a bare `SET content` would be reverted by the next
    # collab open.
    await set_content(doc_id, content, persist=True)
    await rebuild_doc_mentions(db, "documents", doc_id, content)
    await event_bus.emit("content_flushed", entity_type="doc", entity_id=doc_id,
                         project_id=project_id)


async def _create_page(
    project_id: str, page: HelpPage, content: str, parent_id: str | None, sort_key: str,
) -> None:
    doc_id = help_doc_id(project_id, page.slug)
    # WHY: the path carries the doc id, not the slug — a guide moved in from another
    # project keeps its own path, so it can never collide on idx_documents_path with
    # this project's guide.
    await create_document(doc_id, {
        "project_id": project_id, "parent_id": parent_id, "title": page.title,
        "content": content, "path": f".lore/help/{doc_id}", "is_index": False,
        "sort_key": sort_key,
    }, user_name="Lore")


async def sync_project_help(project_id: str, bundle: HelpBundle | None = None) -> None:
    """Create missing guide pages and replace every live one with the shipped text.

    Per page: a row that exists in ANOTHER project was moved out by the user → skip;
    a tombstone → skip (a deletion sticks); a live row → page_update decides; no row
    at all → create it, but only under a live root of this project (or as the root
    itself), so deleting the guide also stops it from growing back.
    """
    bundle = bundle or load_bundle()
    slugs = [p.slug for p in bundle.pages]
    images = list(bundle.images)
    db = await get_db()
    stored = await _stored_pages(db, project_id, slugs)
    root_page = bundle.pages[0]
    root_id = help_doc_id(project_id, root_page.slug)

    created: list[str] = []
    if root_page.slug not in stored:
        await _create_page(
            project_id, root_page, render_links(root_page.body, project_id, slugs, images),
            None, await _root_sort_key(db, project_id),
        )
        created.append(root_id)
        root_live = True
    else:
        row = stored[root_page.slug]
        root_live = row["project_id"] == project_id and row.get("deleted_at") is None

    missing = [p for p in bundle.pages[1:] if p.slug not in stored]
    child_keys = n_keys_between(None, None, len(missing)) if missing else []
    for page, key in zip(missing, child_keys):
        if not root_live:
            break
        await _create_page(project_id, page, render_links(page.body, project_id, slugs, images), root_id, key)
        created.append(help_doc_id(project_id, page.slug))

    await _refresh_stale(db, project_id, bundle, stored)
    if root_live:
        await _restore_order(project_id, bundle)
    await _ensure_images(db, project_id, bundle)

    # WHY: links between guide pages resolve only once every target exists, so the
    # mention edges of freshly created pages are built after the whole batch.
    for doc_id in created:
        rows = await db.query("SELECT content FROM type::record('documents', $id)", {"id": doc_id})
        await rebuild_doc_mentions(db, "documents", doc_id, (rows[0].get("content") if rows else "") or "")


async def _refresh_stale(db, project_id: str, bundle: HelpBundle, stored: dict[str, dict]) -> None:
    """Replace every live page of this project with the shipped title and text."""
    slugs, images = [p.slug for p in bundle.pages], list(bundle.images)
    for page in bundle.pages:
        row = stored.get(page.slug)
        if row is None or row["project_id"] != project_id or row.get("deleted_at") is not None:
            continue
        if row.get("title") != page.title:
            # WHY: through the sole rename path, so open tabs get document_renamed and
            # the title-bearing embeddings are recomputed.
            await rename_document(document_id=row["id"], title=page.title)
        new = page_update(project_id, row.get("content") or "", page.body, slugs, images)
        if new is not None:
            await _refresh(db, row["id"], project_id, new)


async def _restore_order(project_id: str, bundle: HelpBundle) -> None:
    """Put the guide pages under the root back into file order (`NN-` prefix).

    A page shipped in a later release is created with a key that ignores its siblings,
    and a user may have reordered pages; an update restores the shipped order like it
    restores the text. Pages moved elsewhere and the user's own documents under the
    root are left where they are (reordering only moves guide pages among themselves).
    """
    root_id = help_doc_id(project_id, bundle.pages[0].slug)
    siblings = [r["id"] for r in await sibling_rows(project_id, root_id)]
    wanted = [i for i in (help_doc_id(project_id, p.slug) for p in bundle.pages[1:]) if i in siblings]
    current = [i for i in siblings if i in set(wanted)]
    if current == wanted:
        return
    after: str | None = None
    for doc_id in wanted:
        await reorder_document_command(doc_id, after)
        after = doc_id


async def _ensure_images(db, project_id: str, bundle: HelpBundle) -> None:
    """Seed each shipped picture as an image reference of the page that shows it.

    A picture is created only when its row does not exist at all AND a live guide page
    of this project shows it in its CURRENT stored text — so a deleted picture stays
    deleted and a page the user rewrote without it gets no stray reference.
    """
    if not bundle.images:
        return
    alts = {name: alt.strip() for p in bundle.pages for alt, name in _HELP_IMAGE_ALT.findall(p.body)}
    names = [n for n in bundle.images if n in alts]
    if not names:
        return
    in_clause = ",".join(f"type::record('documents', $i{n})" for n in range(len(names)))
    params = {f"i{n}": help_image_id(project_id, name) for n, name in enumerate(names)}
    existing = await db.query(f"SELECT VALUE meta::id(id) FROM documents WHERE id IN [{in_clause}]", params)
    have = set(existing or [])
    stored = await _stored_pages(db, project_id, [p.slug for p in bundle.pages])
    pages = [r for r in stored.values() if r["project_id"] == project_id and r.get("deleted_at") is None]
    for name in names:
        ref_id = help_image_id(project_id, name)
        if ref_id in have:
            continue
        host = next((r["id"] for r in pages if f"(ref:{ref_id})" in (r.get("content") or "")), None)
        if host is None:
            continue
        await _create_image(project_id, ref_id, host, alts[name] or name, bundle.images[name])


async def _create_image(project_id: str, ref_id: str, host_id: str, title: str, src: Path) -> None:
    """Copy one picture into project storage and create its image reference."""
    data = src.read_bytes()
    file_name = f"{src.stem}.jpg"
    rel_path = f"{project_id}/{ref_id}/{file_name}"
    target = STORAGE_PATH / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    with Image.open(src) as img:
        width, height = img.size
    await create_reference_row(
        ref_id=ref_id, project_id=project_id, host_id=host_id, title=title,
        media_type="image", file_path=rel_path,
        file_meta={"mime_type": "image/jpeg", "file_size": len(data),
                   "original_name": file_name, "width": width, "height": height},
    )
    await jobs_pool.enqueue("thumbnail_task", project_id, ref_id, rel_path, job_id=f"thumb:{ref_id}")


async def sweep_help_subtrees(bundle: HelpBundle | None = None) -> int:
    """Sync the guide into every live project once per shipped bundle; return how many.

    A no-op (returns 0) when `app_meta:help_bundle.digest` already equals this bundle —
    one sweep per content release, zero cost on every other boot. The digest is stored
    only after every project synced, so a sweep that fails midway runs again on the
    next boot.
    """
    bundle = bundle or load_bundle()
    digest = bundle.digest()
    db = await get_db()
    meta = await db.query("SELECT digest FROM app_meta:help_bundle")
    if meta and meta[0].get("digest") == digest:
        return 0
    projects = await db.query("SELECT VALUE meta::id(id) FROM projects WHERE deleted_at IS NONE")
    failed: list[str] = []
    for project_id in projects or []:
        # WHY: one broken project must not keep every other project on the old guide;
        # it is logged and the digest is withheld, so the next boot retries the sweep.
        try:
            await sync_project_help(project_id, bundle)
        except Exception:
            logger.warning("help subtree sync failed for project %s", project_id, exc_info=True)
            failed.append(project_id)
    if failed:
        logger.warning("help subtree sync failed in %d of %d projects; retried on next boot",
                       len(failed), len(projects or []))
        return len(projects or []) - len(failed)
    await db.query("UPSERT app_meta:help_bundle SET digest = $d", {"d": digest})
    logger.info("help subtree synced into %d projects", len(projects or []))
    return len(projects or [])
