"""One digest run: config → cursor → material → one LLM call → one report document."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from chat_sessions.recall_search import Window
from documents.service import create_document, effective_parent_id

from db import extract_id, fetch_one, get_db
from event_bus import emit
from pipeline.core.config import _parse_yaml_from_code_block
from pipeline.digest.binding import DigestBinding, load_binding
from pipeline.digest.material import (
    PREVIOUS_REPORT_CHARS,
    Material,
    collect,
    previous_report,
    window_end,
)
from pipeline.extractor.runner import _fetch_user_timezone, _make_pipeline_note
from pipeline.extractor.utils import call_llm_structured, render_title_template

logger = logging.getLogger(__name__)

DEFAULT_DIGEST_TITLE_TEMPLATE = "digest-{yyyy}-{mm}-{dd} {title}"

REPORT_SCHEMA = {
    "type": "object",
    "properties": {"report": {"type": "string"}},
    "required": ["report"],
    "additionalProperties": False,
}

_PROMPT = (
    "You write a periodic digest on one theme for a small group. Below is the theme, "
    "the previous digest, and the material that appeared since it. Write the new digest "
    "in Markdown: what is new, and what changed relative to the previous digest. Use only "
    "material relevant to the theme and ignore the rest. Each chat line starts with its "
    "author's name in bold: attribute every fact to that person by that name. When the "
    "previous digest section holds a digest, compare against its facts; only when it "
    "reads (none) is this the first digest. Write in the language of the material.\n\n"
    "## Theme\n\n{theme}\n\nKeywords: {keywords}\n\n"
    "## Instructions\n\n{instructions}\n\n"
    "## Previous digest\n\n{previous}\n\n"
    "## New references attached to the theme document\n\n{references}\n\n"
    "## New messages in chats on the theme document\n\n{anchored}\n\n"
    "## Related excerpts from other chats\n\n{related}\n\n"
    'Return a JSON object {{"report": "<the digest in Markdown>"}}.'
)

_NONE = "(none)"


def parse_digest_config(content: str, doc_label: str) -> dict:
    """`{theme, keywords, model, instructions}` from a digest config document.

    The first fenced YAML block carries `theme` (required), `keywords`, `model`;
    the prose after the block is the instruction. Raises ValueError.
    """
    parsed = _parse_yaml_from_code_block(content, doc_label=doc_label, unwrap_single_key=False)
    theme = str(parsed.get("theme") or "").strip()
    if not theme:
        raise ValueError(f"{doc_label}: `theme` is required in the YAML block")
    keywords = parsed.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    fence_end = content.find("```", content.find("```") + 3)
    instructions = content[fence_end:].lstrip("`").strip() if fence_end >= 0 else ""
    return {
        "theme": theme,
        "keywords": [str(k) for k in keywords],
        "model": parsed.get("model"),
        "instructions": instructions,
    }


def render_digest_prompt(config: dict, previous: str, material: Material) -> str:
    return _PROMPT.format(
        theme=config["theme"],
        keywords=", ".join(config["keywords"]) or _NONE,
        instructions=config["instructions"] or _NONE,
        previous=previous[:PREVIOUS_REPORT_CHARS] or _NONE,
        references="\n\n".join(material.references) or _NONE,
        anchored="\n\n".join(material.anchored) or _NONE,
        related="\n\n---\n\n".join(material.related) or _NONE,
    )


async def _load_config(config_doc_id: str) -> dict:
    doc = await fetch_one("documents", config_doc_id)
    if not doc or doc.get("deleted_at"):
        raise ValueError(f"Digest config document {config_doc_id} not found")
    return parse_digest_config(doc.get("content") or "", f"digest config {doc.get('title') or config_doc_id}")


async def _write_report(binding: DigestBinding, content: str, until: datetime) -> str:
    theme = await fetch_one("documents", binding.theme_doc_id)
    title = render_title_template(
        binding.title_template or DEFAULT_DIGEST_TITLE_TEMPLATE,
        (theme or {}).get("title") or "Digest",
        tz_name=await _fetch_user_timezone(binding.created_by),
    )
    doc_uid = str(uuid.uuid4())
    # A target of the project's index stores at the root (effective_parent_id).
    parent_id = await effective_parent_id(binding.target_doc_id, binding.project_id)
    record = await create_document(doc_uid, {
        "project_id": binding.project_id,
        "parent_id": parent_id,
        "title": title,
        "content": content,
        "path": f"{doc_uid}.md",
        "is_index": False,
        "digest_config_id": binding.config_id,
        "digest_covered_until": until,
    })
    # Mirrors the extractor's emit — see the INVARIANT on document_created in
    # pipeline/extractor/runner.py (sort_key is required by the client).
    await emit(
        "document_created",
        project_id=binding.project_id,
        document_id=doc_uid,
        title=title,
        parent_id=parent_id,
        sort_key=record.get("sort_key"),
    )
    return doc_uid


async def run_digest(config_id: str, project_id: str) -> dict:
    """Run one digest. Returns `{"status": "empty"}` when nothing is new (no document,
    cursor unchanged) or `{"status": "written", "document_id": …}`. Raises on failure."""
    project_id = extract_id(project_id)
    binding = await load_binding(config_id, project_id)
    config = await _load_config(binding.config_doc_id)
    db = await get_db()
    previous = await previous_report(db, config_id, binding.target_doc_id)
    since = previous["digest_covered_until"] if previous else None
    until = await window_end(db, binding.theme_doc_id, binding.subjects, project_id,
                             since, datetime.now(timezone.utc))
    query = " ".join([config["theme"], *config["keywords"]])
    material = await collect(project_id, binding.theme_doc_id, binding.subjects, query,
                             Window(since, until))
    if material.is_empty():
        logger.info("[Digest] %s: nothing new since %s", config_id, since)
        return {"status": "empty"}
    prompt = render_digest_prompt(config, (previous or {}).get("content") or "", material)
    answer = await call_llm_structured(prompt, REPORT_SCHEMA, model=binding.model or config["model"])
    report = str(answer.get("report") or "").strip()
    if not report:
        raise RuntimeError("The model returned an empty digest")
    doc_uid = await _write_report(binding, report, until)
    logger.info("[Digest] %s wrote %s covering until %s", config_id, doc_uid, until)
    return {"status": "written", "document_id": doc_uid}


async def create_digest_error_note(config_id: str, project_id: str, error: Exception) -> None:
    """The failure trace on the theme document — best-effort, never raises."""
    try:
        row = await fetch_one("agent_configs", config_id)
        theme_doc_id = extract_id((row or {}).get("document_id"))
        if not theme_doc_id:
            logger.error("[Digest] %s failed with no theme document to note on: %s", config_id, error)
            return
        config_doc_id = extract_id(row.get("config_doc_id"))
        config_doc = await fetch_one("documents", config_doc_id) if config_doc_id else None
        title = (config_doc or {}).get("title") or config_doc_id
        await _make_pipeline_note(
            project_id=extract_id(project_id),
            document_id=theme_doc_id,
            title="Pipeline Error",
            body=f"## Digest Error\n\n**Config:** [{title}](doc:{config_doc_id})\n\n```\n{error}\n```",
        )
    except Exception:
        logger.exception("[Digest] error note for %s failed", config_id)
