"""Who wrote a message, as a reader sees it: member, anonymous guest, or the system.

Message authors are three kinds of id on the same column: a `users` record id, an
anonymous note author (`anon_<hash>`, minted by the public note surface — never a
users row), and PIPELINE_AUTHOR_ID. Every reader that turns an author into a label
goes through `author_display`; every batch name lookup goes through `user_names`.
"""

from pipeline.extractor.runner import PIPELINE_AUTHOR_ID

from db import extract_id, validate_record_id
from models import resolve_message_author

ANON_AUTHOR_PREFIX = "anon_"
SYSTEM_AUTHOR_LABEL = "system notes"


def is_anon_author(author_id: str | None) -> bool:
    return bool(author_id) and author_id.startswith(ANON_AUTHOR_PREFIX)


def is_user_author(author_id: str | None) -> bool:
    """Whether the id names a `users` row (neither anonymous nor the system)."""
    return bool(author_id) and author_id != PIPELINE_AUTHOR_ID and not is_anon_author(author_id)


def author_display(message: dict, session: dict, names: dict[str, str]) -> str | None:
    """The label for a message's author; `names` maps user ids to user names.

    # INVARIANT(security): an anonymous author's label is the name stored on THEIR
    # message, never a users lookup.
    # Why: `anon_` ids are not users rows; resolving them as one would either fail
    # or, on an id collision, show a member's name over a stranger's words.
    """
    author = resolve_message_author(message, session)
    if author == PIPELINE_AUTHOR_ID:
        return SYSTEM_AUTHOR_LABEL
    if is_anon_author(author):
        return message.get("author_name") or None
    return names.get(author)


async def user_names(db, author_ids: set[str]) -> dict[str, str]:
    """user id → name for the member ids among `author_ids` (others are skipped)."""
    ids = sorted(a for a in author_ids if is_user_author(a))
    if not ids:
        return {}
    placeholders = ", ".join(f"type::record('users', '{validate_record_id(a)}')" for a in ids)
    users = await db.query(f"SELECT id, name FROM users WHERE id IN [{placeholders}]")
    return {extract_id(u["id"]): u.get("name", "") for u in (users or [])}
