"""Migration: mcp_servers_template_fields_drop — remove the header-template fields.

# ARCH: a server holds one key (auth_mode + auth_scheme), so `headers`,
#   `variables` and user_secrets.var_name have no reader. schema.surql carries
#   the same REMOVEs for fresh DBs; this migration lets the fingerprint guard
#   read the field loss as intentional instead of `removed` drift.

# INVARIANT(persisted): the required `headers`/`variables` must not stay defined
# on a live database.
# Why: rows created without them fail every UPDATE, so saving a key answered 500
# after the key had already been written.

Idempotent (`IF EXISTS`); no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger

_STATEMENTS = (
    "REMOVE FIELD IF EXISTS headers ON mcp_servers",
    "REMOVE FIELD IF EXISTS variables[*].name ON mcp_servers",
    "REMOVE FIELD IF EXISTS variables[*].label ON mcp_servers",
    "REMOVE FIELD IF EXISTS variables[*].help_url ON mcp_servers",
    "REMOVE FIELD IF EXISTS variables[*].secret ON mcp_servers",
    "REMOVE FIELD IF EXISTS variables ON mcp_servers",
    "REMOVE INDEX IF EXISTS idx_user_secrets_server_var ON user_secrets",
    "REMOVE FIELD IF EXISTS var_name ON user_secrets",
)


async def _migrate_mcp_servers_template_fields_drop(db) -> None:
    """Remove the retired header-template definitions (idempotent)."""
    for statement in _STATEMENTS:
        await db.query(statement)
    logger.info("mcp_servers_template_fields_drop: removed the template fields")
