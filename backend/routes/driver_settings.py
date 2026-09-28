"""Driver boot settings — the env the harness boots dsh with, read once at its start.

# ARCH: this is a harness→backend call, not a user surface. It is attested by
# the driver secret (the same `agent.context.driver_attested` the Tool-API uses for the
# approval marker) and never by a session cookie: the response carries a
# plaintext provider key. Values resolve through `settings.get_all`, so the one
# DB-override → env → default layering is the one the harness sees.
"""

from __future__ import annotations

import settings
from agent.context import driver_attested
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/driver", tags=["driver-settings"])

#: WEB_SEARCH_PROVIDER value → (dsh provider id, the setting that is its
#: credential; its key is also the env name the provider reads). `off` has no
#: entry: no pin is served.
_WEB_SEARCH_PROVIDERS: dict[str, tuple[str, str]] = {
    "deepseek": ("deepseek-official", "DEEPSEEK_API_KEY"),
    "brave": ("lore-brave", "BRAVE_API_KEY"),
    "tavily": ("lore-tavily", "TAVILY_API_KEY"),
    "searxng": ("lore-searxng", "SEARXNG_URL"),
}


@router.get("/web-search")
async def web_search_env(
    x_driver_secret: str | None = Header(default=None),
) -> dict[str, str]:
    """Env pairs for the harness: the dsh pin plus ONLY the selected provider's
    credential; `{}` when search is off. An empty credential is still served —
    dsh then fails every call with WEB_PROVIDER_CONFIGURED_UNAVAILABLE."""
    if not await driver_attested(x_driver_secret):
        raise HTTPException(status_code=401, detail="Driver secret required")
    provider = await settings.get("WEB_SEARCH_PROVIDER")
    if provider == "off":
        return {}
    # KeyError is unreachable: `choices` refuses an off-list value at import
    # (env) and at PUT (override).
    pin, credential = _WEB_SEARCH_PROVIDERS[provider]
    value = (await settings.get_all([credential]))[credential]
    return {"DSH_WEB_SEARCH_PROVIDER": pin, credential: value}
