"""# SYSTEM: page-lock — personal PIN-gated page hide (UI + agent).

A user marks some of their pages (e.g. a diary) as locked; the agent in that
user's chats / MCP keys cannot read or find them (read gate + search filter +
chat-context drop), and the UI shows a PIN screen instead of the editor
(frontend, client step of the plan). Protection level: against casual eyes,
NOT against a determined person with DevTools.

The lock is PERSONAL: stored on the user (`users.locked_doc_ids`), not on the
document. Other members see the page as before; hiding from them is RBAC's
job, not this feature's. The PIN is the ACCOUNT PIN (`users.pin_hash`,
verify-pin) — no new secret, no new rate limiter.

Leaf module by design: imports ONLY db, so every choke point (agent read gate,
search filter, chat context, cabinet routes) can reach it without cycles.
"""

from db import fetch_one


async def locked_doc_ids(user_id: str) -> set[str]:
    """The user's locked-page id set; empty when the field is NONE/missing.

    ONE fetch per call (the search filter holds the set for the whole hit
    list, never per hit — the no-gather rule on the shared Surreal conn).
    Ids are stored and compared as plain string ids (the shape search hits
    and tool args carry, i.e. meta::id form).
    """
    row = await fetch_one("users", user_id)
    if not row:
        return set()
    return {str(i) for i in (row.get("locked_doc_ids") or [])}
