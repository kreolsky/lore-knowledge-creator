"""Role-change guard (audit 2.5).

No endpoint that changes users.role exists today. The JWT carries the role for up to
14 days, so any such endpoint MUST call bump_token_version(user_id) or active sessions
keep the old role. This test makes the trap impossible to add silently: it scans backend
for SurrealQL UPDATE statements on the users table that SET role, and fails with a
message pointing to the rule in .claude/rules/backend.md.

This is a TRIPWIRE, not a correlation check: it flags the *pattern* itself, even if a
correct bump_token_version() call sits right next to it. Adding the guard alone will NOT
turn this test green — when a legitimate role-change endpoint is introduced, you must add
bump_token_version(user_id) AND whitelist that call site here (extend ALLOWED_FILES). The
red test is the prompt to do both deliberately.

Creation paths (CREATE / create_record) are exempt — role is legitimately set at birth.
"""

from __future__ import annotations

import pathlib
import re

BACKEND = pathlib.Path("/app")

# Match an UPDATE on the users table whose SET clause assigns role. Covers both
# static strings and f-string bodies (the regex runs over raw source text). The
# table is matched as either `users` or `type::record('users', ...)`.
ROLE_UPDATE_RE = re.compile(
    r"UPDATE\s+(?:type::record\(\s*['\"]users['\"]|users\b)[^;]*?\bSET\b[^;]*?\brole\b\s*=",
    re.IGNORECASE | re.DOTALL,
)

# CREATE-based role assignment is allowed (user creation). Seed/admin creation uses
# create_record("users", ...) which builds a CREATE — not flagged by ROLE_UPDATE_RE.

# Whitelist of files allowed to UPDATE users.role. Empty today (no such endpoint).
# When a legitimate role-change endpoint is added, put bump_token_version(user_id)
# next to the UPDATE, then add the file's path (relative to BACKEND) here.
ALLOWED_FILES: set[str] = set()

RULE_MSG = (
    "An endpoint changes users.role via UPDATE without the required guard. "
    "Per .claude/rules/backend.md: any endpoint that changes users.role MUST call "
    "bump_token_version(user_id) — the JWT carries the role for up to 14 days. "
    "If this UPDATE is intentional: add bump_token_version(user_id) next to it AND "
    "whitelist the file in ALLOWED_FILES in this test (the guard alone won't turn it "
    "green — this is a pattern tripwire, not a correlation check)."
)


def test_no_role_update_without_token_version_bump():
    violations: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(BACKEND).as_posix()
        if rel in ALLOWED_FILES:
            continue
        src = path.read_text()
        if ROLE_UPDATE_RE.search(src):
            violations.append(rel)
    assert not violations, "Role-change UPDATE found without bump_token_version guard:\n" + ", ".join(violations) + f"\n\n{RULE_MSG}"
