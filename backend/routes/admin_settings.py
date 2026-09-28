"""Admin instance-settings routes — registry-derived list, override write, reset.

# ARCH: every route is require_admin — a moderator passes require_user_manager_role
# and reaches /api/admin/users; require_admin is the ONLY gate keeping them off
# this surface, so it is exercised with a moderator principal in the suite.
# The settings surface itself lives in settings_registry (see SYSTEM: instance-settings).
"""

from __future__ import annotations

import asyncio
import logging
import os

import settings
import settings_registry
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from settings_registry import SettingSpec, SettingValueError
from surrealdb import AsyncSurreal

from auth import require_admin
from db import get_db
from event_bus import emit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/settings", tags=["admin-settings"])


def _mask_secret(value: str) -> str:
    """Mask served to the UI: bullets + last 4 chars. Empty secret → empty mask.

    A PUT carrying the mask string back is a no-op in put_setting — the UI
    round-trips masked values without ever re-storing the mask as plaintext.
    """
    if not value:
        return ""
    return "••••" + value[-4:]


def _source(entry: SettingSpec, overridden: bool) -> str:
    if overridden:
        return "override"
    return ".env" if entry.env in os.environ else "default"


async def _entry_payload(entry: SettingSpec, row_value: str | None) -> dict:
    """One registry entry → GET row: metadata + source chip + effective value.

    The effective value of a live key resolves through settings.get — fallback
    walk included — so the row carries exactly what the readers use: an
    override on a BASE key shows through a dependent's row (whose own source
    chip honestly stays `default` — the row itself is un-set). A restart key
    ignores any row: its readers are boot-frozen, so the honest display is the
    backend process's own env-effective value, never an unreachable override.
    That value comes off settings_registry.VALUES — the raw parsed env value,
    immune to config.py's binding wraps (e.g. STORAGE_PATH's Path).
    """
    overridden = row_value is not None and entry.effect == "live"
    if entry.effect == "restart":
        effective = settings_registry.VALUES[entry.key]
    else:
        effective = await settings.get(entry.key)
    if entry.type == "secret":
        effective = _mask_secret(str(effective))
    return {
        "key": entry.key,
        "env": entry.env,
        "tab": entry.tab,
        "section": entry.section,
        "type": entry.type,
        "label": entry.label,
        "help": entry.help,
        "effect": entry.effect,
        "min": entry.min,
        "max": entry.max,
        "choices": list(entry.choices) if entry.choices is not None else None,
        "visible_if": (
            {"key": entry.visible_if[0], "value": entry.visible_if[1]}
            if entry.visible_if is not None else None
        ),
        "source": _source(entry, overridden),
        "value": effective,
    }


@router.get("")
async def list_settings(
    _: dict = Depends(require_admin), db: AsyncSurreal = Depends(get_db),
):
    """The whole editable surface, derived from the registry at call time."""
    by_key = await settings.load_overrides()
    # BY_KEY is THE lookup source (find/settings.get resolve off it); its
    # values() preserves REGISTRY's declaration order, so the listing and the
    # resolver can never disagree about which keys exist.
    settings_rows = await asyncio.gather(
        *(_entry_payload(entry, by_key.get(entry.key))
          for entry in settings_registry.BY_KEY.values())
    )
    return {"settings": list(settings_rows)}


class SettingUpdate(BaseModel):
    """PUT body. `value` is typed by the registry entry (coerce → 422 on mismatch)."""

    value: object


@router.put("/{key}")
async def put_setting(
    key: str,
    body: SettingUpdate,
    user: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Write one override row, drop this process's cache, notify every other process.

    A restart key is refused (422): its readers evaluate at boot, so an override
    row could never take effect — the honest edit is .env + restart.
    """
    entry = settings_registry.find(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown setting key")
    if entry.effect == "restart":
        raise HTTPException(
            status_code=422,
            detail=f"{key} is fixed at process start — set {entry.env} in .env and restart",
        )
    try:
        value = entry.coerce(body.value)
    except SettingValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Masked round-trip: the UI sent the mask back — nothing to change.
    if entry.type == "secret" and body.value == _mask_secret(str(await settings.get(key))):
        row_value = (await settings.load_overrides()).get(key)
        return await _entry_payload(entry, row_value)

    # UPSERT-by-key (not SELECT-then-CREATE): two admins editing concurrently
    # must land on exactly one row per key. `key` is in the SET: a fresh row's
    # non-option column has no DEFAULT, so an UPSERT that omits it creates with
    # NONE and the SCHEMAFULL coerce rejects the write.
    await db.query(
        "UPSERT type::record('instance_settings', $key) SET "
        "key = $key, value = $value, updated_by = $by, updated_at = time::now()",
        {"key": key, "value": entry.encode(value), "by": user["user_id"]},
    )
    settings.drop_cache()
    await emit(settings.SETTINGS_EVENT, key=key)
    logger.info(
        "instance_setting_changed key=%s by=%s overridden=True", key, user["user_id"],
    )
    payload = await _entry_payload(entry, entry.encode(value))
    return payload


@router.delete("/{key}")
async def delete_setting(
    key: str,
    user: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Reset to env/default: the row is REMOVED (never written null), so
    'overridden' stays `row exists`."""
    entry = settings_registry.find(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown setting key")

    rows = await db.query(
        "SELECT VALUE id FROM instance_settings WHERE key = $k", {"k": key},
    )
    if rows:
        await db.query(
            "DELETE type::record('instance_settings', $key)", {"key": key},
        )
        settings.drop_cache()
        await emit(settings.SETTINGS_EVENT, key=key)
        logger.info(
            "instance_setting_changed key=%s by=%s overridden=False", key, user["user_id"],
        )
    return {"key": key, "reset": True, "source": _source(entry, overridden=False)}
