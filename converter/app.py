"""Converter service — stateless DOCX/PDF→Markdown and Markdown→PDF/DOCX export.

# SYSTEM: converter — isolated container (Pandoc + pymupdf4llm + heavy deps) that
# converts between document formats. No DB, no auth, internal Docker network
# only; the backend is the sole client.
"""

import logging

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from convert import docx_bytes_to_markdown, markdown_to_docx, markdown_to_pdf, pdf_bytes_to_markdown

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("converter")

app = FastAPI(title="Lore Converter", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/convert")
async def convert(file: UploadFile = File(...)) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="A filename is required")
    name = file.filename.lower()
    if name.endswith(".docx"):
        engine = docx_bytes_to_markdown
    elif name.endswith(".pdf"):
        engine = pdf_bytes_to_markdown
    else:
        raise HTTPException(status_code=400, detail="Only .docx and .pdf are supported")
    data = await file.read()
    try:
        markdown = engine(data)
    except Exception as e:
        logger.error("Conversion failed for %s: %s", file.filename, e)
        raise HTTPException(status_code=502, detail=f"Conversion failed: {e}")
    return {"markdown": markdown}


@app.post("/export")
async def export(markdown: str = Form(...), format: str = Form(...)) -> Response:
    """Export Markdown to PDF or DOCX. Returns binary response."""
    if format == "docx":
        try:
            data = markdown_to_docx(markdown)
        except Exception as e:
            logger.error("DOCX export failed: %s", e)
            raise HTTPException(status_code=502, detail=f"DOCX export failed: {e}")
        return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    elif format == "pdf":
        try:
            data = markdown_to_pdf(markdown)
        except Exception as e:
            logger.error("PDF export failed: %s", e)
            raise HTTPException(status_code=502, detail=f"PDF export failed: {e}")
        return Response(content=data, media_type="application/pdf")
    else:
        raise HTTPException(status_code=400, detail="format must be pdf or docx")
