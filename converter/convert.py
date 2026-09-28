"""DOCX/PDF→Markdown conversion via Pandoc / pymupdf4llm.

# ARCH: Pure bytes->markdown functions — no DB, no app state, no network out.
# This is the swappable engine boundary: docx_bytes_to_markdown (Pandoc) and
# pdf_bytes_to_markdown (pymupdf4llm) share one contract — GFM markdown, raster
# images inlined as base64 data URIs — so later docling/marker/ML can replace
# either engine without the backend learning anything new. The backend treats
# the result exactly like an uploaded .md. Image forms differ: DOCX emits
# inline `![](data:...)` (one reference per occurrence), PDF emits labelled
# `[img-<hash>]: data:...` definitions with `![image][img-<hash>]` usages —
# the backend's ref-def collapse dedupes to one reference per distinct image.
"""

import base64
import hashlib
import mimetypes
import re
import subprocess
import tempfile
from pathlib import Path

import pymupdf
import pymupdf4llm

# Markdown image links whose target is a local file (not a data: or http(s) URI).
_LOCAL_IMG = re.compile(r"!\[([^\]]*)\]\((?!data:|https?:)([^)]+)\)")


def _inline_media(markdown: str, base_dir: Path) -> str:
    """Replace local image links produced by --extract-media with base64 data URIs.

    Matches the contract of the backend's extract_and_replace_images, which expects
    images inline as data URIs so it can store each as its own reference.
    """
    def repl(m: re.Match) -> str:
        alt, rel = m.group(1), m.group(2).strip()
        path = (base_dir / rel).resolve()
        if not path.is_file() or not path.is_relative_to(base_dir.resolve()):
            return m.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"![{alt}](data:{mime};base64,{b64})"

    return _LOCAL_IMG.sub(repl, markdown)


def docx_bytes_to_markdown(data: bytes) -> str:
    """Convert .docx bytes to GFM Markdown with inline base64 images and LaTeX math."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "in.docx"
        src.write_bytes(data)
        # -raw_html forces images to Markdown ![]() syntax instead of <img> tags
        # (Pandoc emits raw HTML <img> when a picture carries width/height). The
        # backend's extract_and_replace_images only parses Markdown image syntax.
        proc = subprocess.run(
            ["pandoc", str(src), "-f", "docx", "-t", "gfm-raw_html",
             "--wrap=none", f"--extract-media={tmp_dir}"],
            cwd=tmp_dir, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pandoc failed: {proc.stderr[:500]}")
        return _inline_media(proc.stdout, tmp_dir)


# Measured on a 519-page book: min-side<100 drops 513 of 1769 XObjects — repeated
# long thin margin strips (80x684 x111) — while a real-figure cliff starts at <300.
# Hence min-SIDE, not area: the junk is long and thin.
_PDF_MIN_IMAGE_SIDE = 100


def _collect_pdf_images(doc: pymupdf.Document) -> tuple[list[list[str]], dict[str, str]]:
    """Gather surviving raster images per page, deduped by content hash.

    Returns (per_page_labels, definitions) where definitions maps each distinct
    image's label to its data URI (first occurrence wins, later occurrences
    reuse the label). Images whose smaller side is under _PDF_MIN_IMAGE_SIDE are
    dropped; non-png/jpeg payloads are re-encoded to PNG because the backend's
    IMAGE_MIMES whitelist rejects anything else.
    """
    definitions: dict[str, str] = {}
    per_page: list[list[str]] = [[] for _ in range(doc.page_count)]
    for page in doc:
        seen_xrefs: set[int] = set()
        for item in page.get_images(full=True):
            xref = item[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                info = doc.extract_image(xref)
            except Exception:
                continue
            if min(int(info["width"]), int(info["height"])) < _PDF_MIN_IMAGE_SIDE:
                continue
            if info["ext"] in ("png", "jpeg"):
                payload, mime = info["image"], f"image/{info['ext']}"
            else:
                try:
                    pix = pymupdf.Pixmap(doc, xref)
                    if pix.n - pix.alpha > 3:  # CMYK and friends: force RGB
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    payload, mime = pix.tobytes("png"), "image/png"
                except Exception:
                    continue
            label = "img-" + hashlib.sha256(payload).hexdigest()[:16]
            if label not in definitions:
                b64 = base64.b64encode(payload).decode("ascii")
                definitions[label] = f"data:{mime};base64,{b64}"
            per_page[page.number].append(label)
    return per_page, definitions


def pdf_bytes_to_markdown(data: bytes) -> str:
    """Convert .pdf bytes to GFM Markdown with labelled base64 image definitions.

    Text and tables come from pymupdf4llm with its own image writing disabled:
    its image_size_limit is a fraction-of-page rule, not our pixel rule, and its
    write-to-disk mode would bypass the hashing/dedup done in _collect_pdf_images.
    Vector graphics are NOT extracted (operator-accepted; page rasterization is
    a separate task).
    """
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        chunks = pymupdf4llm.to_markdown(doc, write_images=False, page_chunks=True)
        per_page, definitions = _collect_pdf_images(doc)

    page_blocks: list[str] = []
    for i, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        usages = [f"![image][{label}]" for label in per_page[i]]
        block = "\n\n".join(p for p in (text, *usages) if p)
        if block:
            page_blocks.append(block)
    markdown = "\n\n".join(page_blocks)
    if definitions:
        defs = "\n".join(f"[{label}]: {uri}" for label, uri in definitions.items())
        markdown = f"{markdown}\n\n{defs}" if markdown else defs
    return markdown


def markdown_to_docx(md: str) -> bytes:
    """Convert GFM Markdown to .docx bytes via Pandoc."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "in.md"
        src.write_text(md, encoding="utf-8")
        out = tmp_dir / "out.docx"
        proc = subprocess.run(
            ["pandoc", str(src), "-f", "gfm", "-t", "docx", "-o", str(out)],
            cwd=tmp_dir, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pandoc docx export failed: {proc.stderr[:500]}")
        return out.read_bytes()


def markdown_to_pdf(md: str) -> bytes:
    """Convert GFM Markdown to PDF bytes via Pandoc + XeLaTeX."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "in.md"
        src.write_text(md, encoding="utf-8")
        out = tmp_dir / "out.pdf"
        proc = subprocess.run(
            ["pandoc", str(src), "-f", "gfm",
             "-o", str(out),
             "--pdf-engine=xelatex",
             "-V", "mainfont=DejaVu Sans"],
            cwd=tmp_dir, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pandoc pdf export failed: {proc.stderr[:500]}")
        return out.read_bytes()
