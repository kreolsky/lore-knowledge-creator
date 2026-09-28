"""Shared APIRouter for the Tool-API subsystem.

Mirrors routes/chat/_router.py: a tiny module holds the one shared router; the
domain modules (reads/edits/creates/proposals) import it and decorate handlers
onto it; routes/tool_api/__init__.py imports them for their side effects.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/tool", tags=["tool-api"])
