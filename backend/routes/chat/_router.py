"""Shared APIRouter for the chat subsystem."""
from fastapi import APIRouter

router = APIRouter(prefix="/api/chat", tags=["chat"])
