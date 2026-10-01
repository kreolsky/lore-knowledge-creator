"""Lore guide tasks — see SYSTEM: help-subtree."""
from __future__ import annotations


async def help_seed_task(ctx, project_id: str) -> None:
    """Seed the Lore guide into one newly created project."""
    from help_subtree import sync_project_help

    await sync_project_help(project_id)


async def help_sweep_task(ctx) -> None:
    """Bring the guide up to the shipped version in every project (once per bundle)."""
    from help_subtree import sweep_help_subtrees

    await sweep_help_subtrees()
