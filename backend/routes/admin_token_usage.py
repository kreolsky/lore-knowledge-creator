"""Admin token usage report — the day × user × model × source fact cube (see SYSTEM: token-usage)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from token_usage import usage_cube

from auth import require_admin

router = APIRouter(prefix="/api/admin/token-usage", tags=["admin-token-usage"])

MAX_RANGE_DAYS = 366


@router.get("")
async def get_token_usage(
    day_from: date = Query(alias="from"),
    day_to: date = Query(alias="to"),
    _: dict = Depends(require_admin),
) -> dict:
    """`{rows:[{day, user_id, model, source, input, output, cache_read, cache_write}],
    users:{id:label}, groups:{id:{name, members}}, oldest_day}`; days are UTC."""
    if day_to < day_from or (day_to - day_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(400, f"Range must be 1..{MAX_RANGE_DAYS} days with from <= to")
    return await usage_cube(day_from.isoformat(), day_to.isoformat())
