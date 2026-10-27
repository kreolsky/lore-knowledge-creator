"""Lore subtree ⇄ Markdown-folder zip (GitHub/Obsidian-compatible layout).

# SYSTEM: subtree-md — the tree ⇄ folder mapper both MCP tools share:
# export_subtree_md (subtree → staged zip + signed URL) and import_subtree_md
# (zip → subtree under a parent). This module owns the DOMAIN half (walk,
# naming, translation, zip build / zip walk, link relinking); the handlers in
# routes/tool_api/subtree.py own auth + the staged-artifact transport
# (mcp_gateway/staging.py).

# ARCH: export reuses the single-doc export donors instead of rebuilding them:
# the live-session snapshot pattern (documents/service.py export_document —
# expand_tables over the active session's ydoc, else the DB row, which the
# flush pipeline already stores GFM-expanded), and the transclusion/project
# wall semantics of _inline_transclusions (one level deep, project equality —
# an outside target is inlined as text, never leaked as a link).

# Dedup discipline: the naming/dedup pass runs FIRST over the whole tree and
# link rewriting consumes its OUTPUT — links point at actual exported paths,
# never at titles. Titles are not unique in Lore, but filenames are per
# directory, which is what makes relative links resolvable on re-import.

Round-trip losses accepted by design: document ids, manual sibling order
(export is alphabetical), comments/history/permissions, links and embeds
pointing outside the subtree, empty leaves, non-inline attachments.
"""
from __future__ import annotations

import io
import mimetypes
import posixpath
import re
import stat as stat_mod
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

import settings
from agent import doc_state
from agent.collab_writes import create_document_via_collab
from collab.registry import get_active_session
from fastapi import HTTPException
from files_util import mime_for_filename, save_upload, validate_magic
from markdown_normalize import normalize_list_spacing, normalize_markdown

import event_bus
from config import STORAGE_PATH
from db import (
    SAFE_ID_RE,
    fetch_one,
    get_db,
    get_descendant_ids,
    record_refs,
)
from mentions import rebuild_doc_mentions
from table_serialize import expand_tables, import_gfm_tables
from transclusion_grammar import parse_target
from ydoc_store import set_content

# ─── filesystem naming ────────────────────────────────────────────────────────

_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_NAME_LEN = 120

#: Entry names an import must never mistake for parent content (INDEX/README)
#: or for the assets bucket. Compared on the sanitized stem, case-insensitive.
#: `.obsidian` is deliberately absent: sanitize_name strips the leading dot, so
#: no sanitized stem can carry it, and the importer skips `.obsidian/` dirs at
#: READ time (`_SKIP_DIRS`) — the two guards cannot meet.
RESERVED_STEMS = frozenset({"index", "readme", "_assets"})


def sanitize_name(title: str) -> str:
    """Filesystem-safe single-line name from a document title."""
    s = _ILLEGAL_FS.sub(" ", title or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return (s[:_MAX_NAME_LEN] or "Untitled").strip()


def _stem_for(title: str) -> str:
    """Sanitized stem, with reserved entry names escaped (`INDEX` → `INDEX (doc)`).

    # WHY: an exported name that collides with a reserved entry would be
    # silently re-interpreted on import — a doc titled INDEX would land as its
    # PARENT's content, a doc titled _assets as the assets bucket. The suffix
    # keeps the title readable and the round-trip honest (the doc re-imports
    # as `INDEX (doc).md`)."""
    stem = sanitize_name(title)
    if stem.lower() in RESERVED_STEMS:
        stem = f"{stem} (doc)"
    return stem


def _posix_relpath(target: str, start_dir: str) -> str:
    return posixpath.relpath(target, start_dir or ".")


def _rel_link(from_dir: str, to_file: str) -> str:
    """URL-encoded relative markdown path from a file in `from_dir` to
    `to_file` (renders on GitHub AND Obsidian)."""
    rel = _posix_relpath(to_file, from_dir)
    if not rel.startswith("."):
        rel = f"./{rel}"
    return quote(rel, safe="/")


class _Dedup:
    """Per-directory case-insensitive filename dedup (`Title` → `Title (2)`)."""

    def __init__(self) -> None:
        self._used: dict[str, set[str]] = {}

    def unique(self, dir_path: str, stem: str) -> str:
        used = self._used.setdefault(dir_path, set())
        if stem.lower() not in used:
            used.add(stem.lower())
            return stem
        n = 2
        while f"{stem} ({n})".lower() in used:
            n += 1
        candidate = f"{stem} ({n})"
        used.add(candidate.lower())
        return candidate


# ─── export: tree walk + naming plan ──────────────────────────────────────────


@dataclass
class ExportNode:
    doc_id: str
    title: str
    is_dir: bool
    #: zip-relative DIRECTORY this doc's file lives in ("" for the zip root).
    dir_path: str
    #: zip-relative FILE path (`Title.md` leaf / `Title/README.md` parent).
    file_path: str
    #: zip-relative assets dir for this doc's inline images (a LEAF shares its
    #: parent dir's bucket — a file cannot own a directory).
    assets_dir: str
    content: str = ""


async def _read_doc_content(doc_id: str, row: dict) -> str:
    """Live-session snapshot first (tables expanded from the session's ydoc),
    else the DB row — the single-doc export pattern, never only the row."""
    session = get_active_session("doc", doc_id)
    if session is not None and session.clients:
        return expand_tables(session.ydoc)
    return row.get("content") or ""


async def _subtree_children(
    root_id: str, project_id: str,
) -> tuple[dict, dict[str, list[dict]]]:
    """Root row (validated in-project) + live child-doc rows grouped by parent.

    References are not tree children — an export covers documents only."""
    ids = [root_id] + await get_descendant_ids(root_id, project_id)
    db = await get_db()
    refs, params = record_refs("documents", ids)
    rows = await db.query(
        "SELECT meta::id(id) AS id, parent_id, project_id, title, content, "
        f"is_reference FROM documents WHERE id IN [{refs}] AND deleted_at IS NONE",
        params,
    )
    by_id = {r["id"]: r for r in (rows or [])}
    root_row = by_id.get(root_id)
    if root_row is None or root_row.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Document not found")

    children: dict[str, list[dict]] = {}
    for doc_id in ids[1:]:
        row = by_id.get(doc_id)
        if row is None or row.get("is_reference"):
            continue
        parent = row.get("parent_id")
        if isinstance(parent, str) and parent:
            children.setdefault(parent, []).append(row)
    for kids in children.values():
        kids.sort(key=lambda r: (sanitize_name(r.get("title") or "").lower(),
                                 str(r.get("id"))))
    return root_row, children


async def _visit_export_node(row: dict, dir_path: str, *, children: dict,
                             plan: dict, dedup: _Dedup) -> None:
    """Name one doc and recurse: a parent becomes `Title/` + `README.md`, a
    leaf `Title.md`; an empty leaf is skipped entirely."""
    doc_id = str(row["id"])
    kids = children.get(doc_id, [])
    content = await _read_doc_content(doc_id, row)
    if not kids and not content.strip():
        return  # empty leaf: skipped entirely
    stem = dedup.unique(dir_path, _stem_for(row.get("title") or ""))
    if kids:
        node_dir = f"{dir_path}{stem}/"
        plan[doc_id] = ExportNode(
            doc_id=doc_id, title=row.get("title") or "Untitled",
            is_dir=True, dir_path=dir_path, file_path=f"{node_dir}README.md",
            assets_dir=f"{node_dir}_assets", content=content,
        )
        for kid in kids:
            await _visit_export_node(kid, node_dir, children=children,
                                     plan=plan, dedup=dedup)
    else:
        plan[doc_id] = ExportNode(
            doc_id=doc_id, title=row.get("title") or "Untitled",
            is_dir=False, dir_path=dir_path, file_path=f"{dir_path}{stem}.md",
            assets_dir=f"{dir_path}_assets" if dir_path else "_assets",
            content=content,
        )


async def plan_export(root_id: str, project_id: str) -> dict[str, ExportNode]:
    """Walk the subtree and produce the naming plan (doc_id → node), in tree
    order. Empty leaves are skipped entirely; a parent with empty content
    keeps its dir but its README.md is omitted at zip time. Siblings are
    alphabetical — manual order is an accepted round-trip loss."""
    root_row, children = await _subtree_children(root_id, project_id)
    plan: dict[str, ExportNode] = {}
    await _visit_export_node(root_row, "", children=children, plan=plan,
                             dedup=_Dedup())
    if not plan:
        raise HTTPException(
            status_code=400,
            detail="Subtree holds nothing to export — the root is an empty leaf",
        )
    return plan


# ─── export: content translation ──────────────────────────────────────────────

_EMBED_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
# The lookbehind keeps the pass off image syntax — `![alt](...)` is the embed
# pass's grammar; a plain link never starts with the bang.
_LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)]+)\)")
_DIMS_RE = re.compile(r"\|\d+x\d+$")

_MEDIA_DROP = frozenset({"audio", "video", "file"})


def _strip_dims(alt: str) -> str:
    """Drop the `|WxH` dimension suffix from an image alt (dims do not survive
    a folder layout; the alt text does)."""
    return _DIMS_RE.sub("", alt)


async def _cached_row(cache: dict, doc_id: str) -> dict | None:
    if doc_id not in cache:
        cache[doc_id] = await fetch_one("documents", doc_id)
    return cache[doc_id]


def _read_stored_bytes(row: dict) -> bytes | None:
    """An in-project reference's file bytes, containment-checked against the
    storage root (mirror of the single-doc export's base64 pass)."""
    rel_path = row.get("file_path", "")
    if not rel_path:
        return None
    abs_path = (STORAGE_PATH / rel_path).resolve()
    if not abs_path.is_relative_to(STORAGE_PATH.resolve()) or not abs_path.is_file():
        return None
    return abs_path.read_bytes()


class _AssetSink:
    """Collects inline-image binaries per node dir and mints unique asset
    filenames (`pic.png`, `pic (2).png`)."""

    def __init__(self) -> None:
        #: zip-relative asset path → bytes
        self.files: dict[str, bytes] = {}
        #: (assets dir, ref id) → link-relative name — the SAME binary linked
        #: from several docs of one directory is staged ONCE and every link
        #: points at the same file.
        self._by_ref: dict[tuple[str, str], str] = {}

    def add(self, node: ExportNode, ref_id: str, ref_row: dict) -> str | None:
        """Stage one image ref's binary under the node's assets dir; returns
        the link-relative name (`_assets/pic.png`), or None when the binary is
        unreadable (the embed then stays verbatim)."""
        base = f"{node.assets_dir}/" if node.assets_dir else ""
        if not base:
            return None
        seen = self._by_ref.get((base, ref_id))
        if seen is not None:
            return seen
        data = _read_stored_bytes(ref_row)
        if data is None:
            return None
        meta = ref_row.get("file_meta") or {}
        original = str(meta.get("original_name") or "")
        stem = sanitize_name(Path(original).stem) or ref_id[:8]
        ext = Path(original).suffix or (
            mimetypes.guess_extension(str(meta.get("mime_type") or "")) or ".png"
        )
        existing = {PurePosixPath(p).name.lower() for p in self.files
                    if p.startswith(base)}
        candidate = f"{stem}{ext}"
        if candidate.lower() in existing:
            n = 2
            while f"{stem} ({n}){ext}".lower() in existing:
                n += 1
            candidate = f"{stem} ({n}){ext}"
        self.files[f"{base}{candidate}"] = data
        rel = f"_assets/{candidate}"
        self._by_ref[(base, ref_id)] = rel
        return rel


async def _embed_replacement(alt: str, target: str, node: ExportNode,
                             plan: dict[str, ExportNode], project_id: str,
                             row_cache: dict, assets: _AssetSink) -> str | None:
    """The replacement text for one embed, or None = leave verbatim.

    Split rule (plan decision): a target INSIDE the exported subtree becomes a
    relative `![alt](./X.md)` (an Obsidian live embed); a target outside is
    resolved and INLINED as its text one level deep, with the same project
    wall and media rules as the single-doc export (images → assets binary,
    non-image media → dropped, text-bearing → text)."""
    parsed = parse_target(target)
    if parsed is None or parsed["scheme"] == "table":
        # Non-transclusion targets stay as authored; `table:` anchors were
        # already GFM-expanded at read time — a stray one stays verbatim.
        return None
    tid = parsed["id"]
    if parsed["scheme"] in ("doc", "bare-doc") and tid in plan:
        return f"![{alt}]({_rel_link(node.dir_path, plan[tid].file_path)})"

    row = await _cached_row(row_cache, tid)
    if row is None or row.get("deleted_at") or row.get("project_id") != project_id:
        return None
    if row.get("is_reference"):
        media = row.get("media_type")
        if media == "image":
            rel = assets.add(node, tid, row)
            if rel is None:
                return None
            return f"![{_strip_dims(alt)}]({rel})"
        if media in _MEDIA_DROP:
            return ""
        body = row.get("content")
        return body if (body and body.strip()) else None
    body = row.get("content") or ""
    return body if body.strip() else None


def _link_replacement(text: str, target: str, node: ExportNode,
                      plan: dict[str, ExportNode]) -> str | None:
    """The replacement for one `[text](target)` mention, or None = verbatim.

    A doc target inside the subtree becomes a relative link; a doc/ref target
    outside it (or a dead id) becomes PLAIN TEXT — references are never
    exported, and a dangling relative link is worse than no link."""
    parsed = parse_target(target)
    if parsed is None or parsed["scheme"] == "table":
        return None
    tid = parsed["id"]
    if parsed["scheme"] in ("doc", "bare-doc"):
        if tid in plan:
            return f"[{text}]({_rel_link(node.dir_path, plan[tid].file_path)})"
        if SAFE_ID_RE.match(tid):
            return text
        return None
    if parsed["scheme"] == "ref" and SAFE_ID_RE.match(tid):
        return text
    return None


async def _translate_content(content: str, node: ExportNode,
                             plan: dict[str, ExportNode], project_id: str,
                             row_cache: dict, assets: _AssetSink) -> str:
    """One document's stored content → the exported file body: spacing
    normalized (the single-doc export entry rule), embeds and mentions mapped
    onto the folder layout."""
    content = normalize_list_spacing(content)

    embed_out: list[str] = []
    pos = 0
    for m in _EMBED_RE.finditer(content):
        embed_out.append(content[pos:m.start()])
        repl = await _embed_replacement(
            m.group(1), m.group(2), node, plan, project_id, row_cache, assets,
        )
        embed_out.append(m.group(0) if repl is None else repl)
        pos = m.end()
    embed_out.append(content[pos:])
    content = "".join(embed_out)

    def _link_sub(m: re.Match) -> str:
        repl = _link_replacement(m.group(1), m.group(2), node, plan)
        return m.group(0) if repl is None else repl

    return _LINK_RE.sub(_link_sub, content)


# ─── export: zip build + entry point ──────────────────────────────────────────


async def export_subtree(*, root_id: str, project_id: str) -> dict:
    """Subtree → zip bytes + counts. Caller owns auth, scope and staging."""
    plan = await plan_export(root_id, project_id)
    row_cache: dict = {}
    assets = _AssetSink()
    for node in plan.values():
        node.content = await _translate_content(
            node.content, node, plan, project_id, row_cache, assets,
        )

    entries: dict[str, bytes] = {}
    for node in plan.values():
        # README.md is omitted for an empty parent — the dir stays for children.
        if node.content.strip():
            entries[node.file_path] = node.content.encode("utf-8")
    entries.update(assets.files)

    max_mb = await settings.get("MAX_ARCHIVE_SIZE_MB")
    total = sum(len(v) for v in entries.values())
    if total > max_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail=f"Export too large (max {max_mb}MB)",
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    return {
        "root_id": root_id,
        "zip_bytes": buf.getvalue(),
        "filename": f"{sanitize_name(plan[root_id].title)}.zip",
        "documents": len(plan),
        "files": len(entries),
    }


# ─── import: zip walk → documents ─────────────────────────────────────────────

# Wiki links: [[Note]], [[Note|alias]], [[Note#heading]]; embeds: ![[Note]].
_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_WIKI_LINK_RE = re.compile(r"(?<!\!)\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

_SKIP_DIRS = frozenset({".obsidian", "__MACOSX"})

#: Total-uncompressed headroom over the archive cap for read_zip_tree's bomb
#: fence. WHY: honest md vaults compress a few-fold (text) or barely at all
#: (images are already compressed), so a declared total past this multiple
#: cannot be real input — it is a decompression bomb, and every accepted
#: entry is read fully into memory.
_DECOMP_HEADROOM = 10


class _ZipTree:
    """The imported archive as a directory tree: `files` maps a filename to
    bytes, `dirs` maps a dirname to a nested _ZipTree. Every accepted path is
    RELATIVE, separator-normalized and free of `..`/symlinks (zip-slip is
    rejected at read time, not papered over at walk time)."""

    def __init__(self) -> None:
        self.dirs: dict[str, "_ZipTree"] = {}
        self.files: dict[str, bytes] = {}


def _reject_zip_path(name: str) -> None:
    """Raise 400 on a zip entry that could escape the staging semantics.

    # INVARIANT(security): zip-slip — `..`/absolute/backslash paths and
    # symlink entries are rejected, never normalized. Why: an archive name is
    attacker-controlled; a normalized '..' still names a parent on write, and
    a symlink entry's bytes are a target path, not content."""
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
        or ".." in PurePosixPath(name).parts
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Archive entry has an invalid path: {name!r}",
        )


def read_zip_tree(zip_bytes: bytes) -> _ZipTree:
    """Validate + read the archive into a _ZipTree (Obsidian-shaped vault).

    `.obsidian/` (and `__MACOSX/`) entries are skipped: settings files are a
    documented non-goal of the import. Entries whose DECLARED uncompressed
    size busts the archive cap — per entry, or ×``_DECOMP_HEADROOM`` in
    total — are refused BEFORE any read (a decompression-bomb fence: every
    accepted entry is read fully into memory).
    """
    root = _ZipTree()
    # WHY bootstrap_value (the sync env⊕default leg, the sweep_staging
    # precedent): read_zip_tree is sync, and the compressed side was already
    # live-capped at redeem — this is a fence, not a user-facing quota.
    cap_mb = settings.bootstrap_value("MAX_ARCHIVE_SIZE_MB")
    cap = cap_mb * 1024 * 1024
    declared_total = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if stat_mod.S_ISLNK(mode):
                raise HTTPException(
                    status_code=400,
                    detail=f"Archive entry is a symlink: {info.filename!r}",
                )
            name = info.filename
            if name.endswith("/"):
                continue
            _reject_zip_path(name)
            parts = PurePosixPath(name).parts
            if any(p in _SKIP_DIRS for p in parts[:-1]):
                continue
            if info.file_size > cap:
                raise HTTPException(
                    status_code=413,
                    detail=f"Archive entry too large decompressed "
                           f"(max {cap_mb}MB): {name!r}",
                )
            declared_total += info.file_size
            if declared_total > cap * _DECOMP_HEADROOM:
                raise HTTPException(
                    status_code=413,
                    detail=f"Archive decompresses beyond the allowed total "
                           f"(max {cap_mb * _DECOMP_HEADROOM}MB)",
                )
            data = zf.read(info)
            node = root
            for part in parts[:-1]:
                node = node.dirs.setdefault(part, _ZipTree())
            node.files[parts[-1]] = data
    return root


def _decode_md(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail=f"{name} is not valid UTF-8 text",
        )


async def _prevalidate_tree(root: _ZipTree) -> None:
    """Reject every CONTENT the apply passes would refuse — BEFORE a row exists.

    Pass A creates rows as it walks; a content gate firing mid-apply leaves
    half a subtree with no rollback. Everything knowable without a host
    document is decided here: md size / UTF-8 / remote images / empty md stem
    (the pass-B text gates, on the same bytes), and the `_assets` binaries'
    image/archive caps + magic (the _AssetImporter gates). Residual, accepted:
    a binary OUTSIDE `_assets/` that a document links relatively is still
    imported — and gated — mid-apply."""
    max_md_mb = await settings.get("MAX_MARKDOWN_SIZE_MB")
    max_img_mb = await settings.get("MAX_IMAGE_SIZE_MB")
    max_arc_mb = await settings.get("MAX_ARCHIVE_SIZE_MB")

    def _walk(node: _ZipTree, dir_path: str, in_assets: bool) -> None:
        for name, sub in node.dirs.items():
            _walk(sub, f"{dir_path}{name}/", name == "_assets")
        for name, data in node.files.items():
            path = f"{dir_path}{name}"
            if name.lower().endswith((".md", ".markdown")):
                if not name.rsplit(".", 1)[0]:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Markdown file has an empty name: {path!r}",
                    )
                if len(data) > max_md_mb * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {max_md_mb}MB): {path}",
                    )
                doc_state.reject_remote_images(_decode_md(data, path))
            elif in_assets:
                mime = mime_for_filename(name)
                cap_mb = max_img_mb if mime.startswith("image/") else max_arc_mb
                if len(data) > cap_mb * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large (max {cap_mb}MB): {path}",
                    )
                if mime.startswith("image/") and not validate_magic(data, mime):
                    raise HTTPException(
                        status_code=400,
                        detail="File content does not match declared type: "
                               f"{name}",
                    )

    _walk(root, "", False)


@dataclass
class _ImportDoc:
    """One planned document: where it lives in the zip and what feeds it."""

    doc_id: str
    title: str
    #: zip dir this doc's FILE sits in ("" for the archive root).
    dir_path: str
    #: raw md text (None for a dir whose INDEX/README is absent).
    text: str | None
    tables_json: str = "{}"


async def _create_import_docs(
    root: _ZipTree, *, project_id: str, parent_id: str | None,
    user: dict, scope_root: str | None,
) -> tuple[list[_ImportDoc], dict[str, str], str | None]:
    """Pass A: create every doc row EMPTY (pass B writes final content).

    Returns (docs, path→doc_id map for every importable file/dir, root_id).
    A dir becomes a document (its INDEX.md/README.md is CONTENT, not a child);
    an `X.md` file becomes a leaf titled `X`; every non-md file stays an asset
    (imported as a reference in pass B, never a document)."""
    state = _ImportState(project_id=project_id, user=user, scope_root=scope_root)
    await _walk_zip_docs(root, "", parent_id, state)
    if not state.docs:
        raise HTTPException(
            status_code=400,
            detail="Archive holds no Markdown files to import",
        )
    root_id: str | None = None
    top_dirs = list(root.dirs)
    if len(top_dirs) == 1 and not root.files:
        root_id = state.paths.get(top_dirs[0])
    return state.docs, state.paths, root_id


@dataclass
class _ImportState:
    """Pass-A mutable context: the created docs (tree order) and the
    zip-path → doc-id map every later pass resolves links against."""

    project_id: str
    user: dict
    scope_root: str | None
    docs: list = field(default_factory=list)
    paths: dict = field(default_factory=dict)

    async def create(self, title: str, parent: str | None, dir_path: str,
                     text: str | None) -> str:
        created = await create_document_via_collab(
            title=title, content="", parent_id=parent, project_id=self.project_id,
            user=self.user, scope_root=self.scope_root,
        )
        doc_id = created["doc_id"]
        self.docs.append(_ImportDoc(doc_id=doc_id, title=title,
                                    dir_path=dir_path, text=text))
        return doc_id


async def _walk_zip_docs(
    node: _ZipTree, dir_path: str, parent: str | None, st: _ImportState,
) -> None:
    """Create a document per directory / md file, depth-first, children
    alphabetical — mirrors the exporter's sibling order. A DIR doc's
    dir_path is its OWN dir (its INDEX.md lives inside it; links and assets
    resolve relative to it). An `_assets` directory is NOT a document — it
    is the binary bucket (its files become references in pass B and via
    _import_unlinked_assets), so the walk skips it entirely."""
    for name in sorted([*node.dirs, *node.files]):
        if name in node.dirs and name == "_assets":
            continue
        child_path = f"{dir_path}{name}"
        if name in node.dirs:
            sub = node.dirs[name]
            text = None
            for index_name in ("INDEX.md", "README.md"):
                if index_name in sub.files:
                    text = _decode_md(sub.files.pop(index_name), index_name)
                    break
            doc_id = await st.create(name, parent, f"{child_path}/", text)
            st.paths[child_path] = doc_id
            # A vault may link the parent's content file directly
            # (`[f](Folder/INDEX.md)`); both reserved names map to the dir's
            # doc (INDEX wins as content by the pop order above).
            st.paths[f"{child_path}/INDEX.md"] = doc_id
            st.paths[f"{child_path}/README.md"] = doc_id
            await _walk_zip_docs(sub, f"{child_path}/", doc_id, st)
        elif name.lower().endswith((".md", ".markdown")):
            doc_id = await st.create(
                name.rsplit(".", 1)[0], parent, dir_path,
                _decode_md(node.files[name], name),
            )
            st.paths[child_path] = doc_id


class _AssetImporter:
    """Pass-B binary channel: zip path → reference, created once and cached.

    An image lands as an image reference (magic-checked, thumbnail enqueued by
    save_upload); anything else lands as a generic `file` reference. The host
    is the document of the containing directory — an asset never dangles at
    the import root."""

    def __init__(self, *, project_id: str, user: dict):
        self.project_id = project_id
        self.user = user
        self.by_path: dict[str, dict] = {}
        self.count = 0

    async def ref_for(self, zip_path: str, data: bytes,
                      host_id: str) -> dict | None:
        if zip_path in self.by_path:
            return self.by_path[zip_path]
        name = PurePosixPath(zip_path).name
        stem = sanitize_name(PurePosixPath(zip_path).stem) or name
        mime = mime_for_filename(name)
        if mime.startswith("image/"):
            cap = await settings.get("MAX_IMAGE_SIZE_MB")
            if len(data) > cap * 1024 * 1024:
                raise HTTPException(413, detail=f"File too large (max {cap}MB)")
            if not validate_magic(data, mime):
                raise HTTPException(
                    400, detail=f"File content does not match declared type: {name}",
                )
            media = "image"
        else:
            cap = await settings.get("MAX_ARCHIVE_SIZE_MB")
            if len(data) > cap * 1024 * 1024:
                raise HTTPException(413, detail=f"File too large (max {cap}MB)")
            mime = mime or "application/octet-stream"
            media = "file"
        ref_id, result = await save_upload(
            data, mime, name, self.project_id, host_id,
            title=stem, media_type=media, processing_status=None,
            created_by=self.user.get("user_id"),
            created_by_name=self.user.get("name"),
        )
        record = {"ref_id": ref_id, "result": result}
        self.by_path[zip_path] = record
        self.count += 1
        return record


def _asset_dims(record: dict) -> str:
    """The `|WxH` dimension suffix an image embed carries (parity with
    extract_and_replace_images); empty for unknown dims."""
    meta = (record.get("result") or {}).get("file_meta") or {}
    w, h = meta.get("width"), meta.get("height")
    return f"{w}x{h}" if w and h else ""


def _resolve_rel_path(link_dir: str, target: str) -> str:
    """A markdown link target → a normalized in-archive path, or "" when it is
    external (scheme/anchor), malformed, or escapes the archive root."""
    decoded = unquote(target).split("#", 1)[0]
    if (
        not decoded
        or "://" in decoded
        or decoded.startswith(("mailto:", "#", "data:"))
        or "\\" in decoded
        or re.match(r"^[A-Za-z]:", decoded)
        or ".." in PurePosixPath(decoded).parts
    ):
        return ""
    combined = posixpath.normpath(posixpath.join(link_dir, decoded))
    if combined.startswith("..") or combined.startswith("/"):
        return ""
    return combined


def _node_files(root: _ZipTree, path: str) -> bytes | None:
    """The bytes of an in-archive file path, or None when it names no file."""
    node = root
    parts = PurePosixPath(path).parts
    for part in parts[:-1]:
        node = node.dirs.get(part)
        if node is None:
            return None
    return node.files.get(parts[-1])


def _find_asset_by_name(root: _ZipTree, filename: str) -> str | None:
    """First in-archive file NAMED `filename` (Obsidian's vault-wide name
    resolution for `![[pic.png]]`), or None. Deterministic: alphabetical
    depth-first walk."""
    def _walk(node: _ZipTree, dir_path: str) -> str | None:
        for name in sorted(node.files):
            if name == filename:
                return f"{dir_path}{name}"
        for name in sorted(node.dirs):
            found = _walk(node.dirs[name], f"{dir_path}{name}/")
            if found:
                return found
        return None

    return _walk(root, "")


async def _sub_async(pattern: re.Pattern, repl, text: str) -> str:
    """regex.sub with an ASYNC repl — matches are awaited in order (the repl
    imports binaries; sequential keeps the shared Surreal connection happy)."""
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        out.append(text[pos:m.start()])
        out.append(await repl(m))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


@dataclass
class _StemEntry:
    stem: str
    dir_path: str
    doc_id: str


def _doc_id_by_stem(index: list[_StemEntry], stem: str,
                    from_dir: str) -> str | None:
    """Wiki resolution: a same-folder match wins, else a UNIQUE stem match in
    the whole import; else None — never a guess among duplicates."""
    same = [e for e in index if e.stem == stem and e.dir_path == from_dir]
    if same:
        return same[0].doc_id
    hits = [e for e in index if e.stem == stem]
    return hits[0].doc_id if len(hits) == 1 else None


async def _rewrite_content(
    text: str, doc: _ImportDoc, root: _ZipTree, paths: dict[str, str],
    index: list[_StemEntry], assets: _AssetImporter, asset_host: str,
) -> str:
    """Pass-B text translation for one document: embeds first
    (_rewrite_embeds), then wiki/relative links (_rewrite_links).

    Relative image links and `![[asset]]` embeds → `![alt|WxH](ref:id)`
    (binaries imported once, hosted on the containing dir's doc); wiki
    links/embeds and relative md links → mention links `[t](<id>)` and doc
    embeds `![alt](doc:<id>)`; everything that does not resolve inside the
    import becomes plain text — external URLs stay verbatim."""
    text = await _rewrite_embeds(text, doc, root, paths, index, assets,
                                 asset_host)
    return _rewrite_links(text, doc, paths, index)


async def _rewrite_embeds(
    text: str, doc: _ImportDoc, root: _ZipTree, paths: dict[str, str],
    index: list[_StemEntry], assets: _AssetImporter, asset_host: str,
) -> str:
    """Embed translation: markdown embeds `![alt](path)` (doc target first,
    then asset binary — an in-archive .md is a DOC embed, not a file), then
    Obsidian wiki embeds `![[Note]]` / `![[pic.png]]`."""
    link_dir = doc.dir_path

    async def _asset_record(path: str) -> dict | None:
        data = _node_files(root, path)
        if data is None:
            return None
        return await assets.ref_for(path, data, asset_host)

    async def _embed_repl(m: re.Match) -> str:
        alt, target = m.group(1), m.group(2)
        path = _resolve_rel_path(link_dir, target)
        if path:
            doc_id = paths.get(path)
            if doc_id is not None:
                return f"![{alt}](doc:{doc_id})"
            record = await _asset_record(path)
            if record is not None:
                dims = _asset_dims(record)
                suffix = f"|{dims}" if dims else ""
                return f"![{_strip_dims(alt)}{suffix}](ref:{record['ref_id']})"
            return m.group(0)
        return m.group(0)

    text = await _sub_async(_EMBED_RE, _embed_repl, text)
    return await _rewrite_wiki_embeds(text, doc, root, index, assets,
                                      asset_host, _asset_record)


async def _rewrite_wiki_embeds(
    text: str, doc: _ImportDoc, root: _ZipTree, index: list[_StemEntry],
    assets: _AssetImporter, asset_host: str, _asset_record,
) -> str:
    """Obsidian wiki embeds: `![[Note]]` → a doc embed; `![[pic.png]]` → the
    named binary imported as an image ref (vault-wide name resolution)."""
    link_dir = doc.dir_path

    async def _wiki_embed_repl(m: re.Match) -> str:
        target = m.group(1).strip()
        if "." in PurePosixPath(target).name and not target.lower().endswith(
            (".md", ".markdown")
        ):
            path = _find_asset_by_name(root, target.split("#", 1)[0])
            if path is not None:
                record = await _asset_record(path)
                if record is not None:
                    dims = _asset_dims(record)
                    suffix = f"|{dims}" if dims else ""
                    stem = sanitize_name(PurePosixPath(target).stem) or target
                    return f"![{stem}{suffix}](ref:{record['ref_id']})"
            return target
        stem = sanitize_name(target.split("#", 1)[0].strip())
        doc_id = _doc_id_by_stem(index, stem, link_dir)
        return f"![{target}](doc:{doc_id})" if doc_id else target

    return await _sub_async(_WIKI_EMBED_RE, _wiki_embed_repl, text)


def _rewrite_links(
    text: str, doc: _ImportDoc, paths: dict[str, str],
    index: list[_StemEntry],
) -> str:
    """Link translation: wiki links `[[X]]`/`[[X|a]]`/`[[X#h]]`, then relative
    md links — both to bare-id mentions; unresolvable targets become plain
    text, a bare doc id and every external target stay verbatim."""
    link_dir = doc.dir_path

    def _wiki_link_repl(m: re.Match) -> str:
        target, alias = m.group(1), m.group(2)
        display = (alias or target).strip()
        stem = sanitize_name(target.split("#", 1)[0].strip())
        doc_id = _doc_id_by_stem(index, stem, link_dir)
        return f"[{display}]({doc_id})" if doc_id else display

    text = _WIKI_LINK_RE.sub(_wiki_link_repl, text)

    def _md_link_repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        # A bare doc id is a Lore mention (authored, or just written by the
        # embed pass) — never an in-archive path; leave it verbatim.
        decoded = unquote(target).split("#", 1)[0]
        if SAFE_ID_RE.match(decoded):
            return m.group(0)
        path = _resolve_rel_path(link_dir, target)
        if not path:
            return m.group(0)  # external URL / anchor — verbatim
        doc_id = paths.get(path)
        return f"[{label}]({doc_id})" if doc_id is not None else label

    return _LINK_RE.sub(_md_link_repl, text)


async def import_subtree_zip(
    zip_bytes: bytes, *, project_id: str, parent_id: str | None,
    user: dict, scope_root: str | None = None,
) -> dict:
    """An md-folder zip → a Lore subtree under `parent_id`. Three phases:

    0 — prevalidate every content gate (no row exists yet, so a rejected
    import leaves no partial subtree); A — create every doc row EMPTY (one
    creation factory, so parent validity, the scope wall and unique paths are
    the shared create path's own gates); B — write final content per doc
    (normalize → GFM tables → asset refs → link rewriting) through set_content
    + the mention rebuild, so links and edges exist only once every node does
    (the help-seeder batch pattern).

    Round-trip losses mirror the exporter: ids, manual order, anything outside
    the archive."""
    root = read_zip_tree(zip_bytes)
    await _prevalidate_tree(root)
    docs, paths, root_id = await _create_import_docs(
        root, project_id=project_id, parent_id=parent_id, user=user,
        scope_root=scope_root,
    )

    index = [_StemEntry(stem=d.title, dir_path=d.dir_path, doc_id=d.doc_id)
             for d in docs]
    assets = _AssetImporter(project_id=project_id, user=user)
    await _apply_imported_content(
        docs, root, paths, index, assets, parent_id=parent_id,
        project_id=project_id,
    )
    await _import_unlinked_assets(root, paths, assets, parent_id)

    return {
        "status": "applied",
        "root_id": root_id,
        "documents": len(docs),
        "references": assets.count,
    }


async def _apply_imported_content(
    docs: list, root: _ZipTree, paths: dict[str, str],
    index: list[_StemEntry], assets: _AssetImporter, *,
    parent_id: str | None, project_id: str,
) -> None:
    """Pass B: write each doc's final content (normalize → GFM tables → asset
    refs → link rewriting) through set_content + the mention rebuild, so links
    and edges exist only once every node does (the help-seeder batch pattern).

    The size/remote-image gates repeat the prevalidation belt-fashion on the
    exact bytes that reach the writer."""
    max_md_mb = await settings.get("MAX_MARKDOWN_SIZE_MB")
    for doc in docs:
        if doc.text is None:
            continue
        raw = doc.text.encode("utf-8")
        if len(raw) > max_md_mb * 1024 * 1024:
            raise HTTPException(
                413, detail=f"File too large (max {max_md_mb}MB): {doc.title}.md",
            )
        text = normalize_markdown(doc.text)
        text, tables_json = import_gfm_tables(text)
        asset_host = _host_for_dir(paths, doc, parent_id)
        text = await _rewrite_content(
            text, doc, root, paths, index, assets, asset_host,
        )
        doc_state.reject_remote_images(text)
        await _finalize_imported_doc(doc, text, tables_json, project_id)


async def _import_unlinked_assets(
    root: _ZipTree, paths: dict[str, str], assets: _AssetImporter,
    parent_id: str | None,
) -> None:
    """Import every remaining file under an `_assets/` directory as a
    reference on that directory's document (linked ones are already staged by
    pass B; a vault's asset folder must survive the import, linked or not).

    A ROOT-level `_assets/` bucket hosts on the import parent — the same
    host-for-dir rule pass B uses for root-level documents."""

    async def _walk(node: _ZipTree, dir_path: str) -> None:
        for name, sub in node.dirs.items():
            await _walk(sub, f"{dir_path}{name}/")
        trimmed = dir_path.rstrip("/")
        if not trimmed:
            return
        if trimmed == "_assets":
            host = parent_id
        elif trimmed.endswith("/_assets"):
            host = paths.get(trimmed.rsplit("/_assets", 1)[0])
        else:
            return
        if not host:
            return
        for name in sorted(node.files):
            await assets.ref_for(f"{dir_path}{name}", node.files[name], host)

    await _walk(root, "")


def _host_for_dir(paths: dict[str, str], doc: _ImportDoc,
                  parent_id: str | None) -> str:
    """The Lore doc that hosts a doc's directory assets: the DIR doc itself
    when one exists at that level, else the import parent."""
    if doc.dir_path:
        host = paths.get(doc.dir_path.rstrip("/"))
        if host is not None:
            return host
    return parent_id or ""


async def _finalize_imported_doc(doc: _ImportDoc, text: str,
                                 tables_json: str, project_id: str) -> None:
    """Write one imported doc's final content + mention edges — the seeder's
    refresh tail: the ONE canonical content writer, then the mention rebuild
    and the embed debounce signal."""
    await set_content(doc.doc_id, text, persist=True,
                      tables_json=tables_json if tables_json != "{}" else None)
    db = await get_db()
    await rebuild_doc_mentions(db, "documents", doc.doc_id, text)
    if text.strip():
        await event_bus.emit("content_flushed", entity_type="doc",
                             entity_id=doc.doc_id, project_id=project_id)
