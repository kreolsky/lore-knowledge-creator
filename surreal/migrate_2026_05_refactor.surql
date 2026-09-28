-- 2026-05 Refactor — References → Documents-with-flag, Notes → Note-Chats
--
-- The data migration is executed by the Python runner:
--   docker compose exec backend python -m migrations.migrate_2026_05_refactor
--
-- Dry-run (copy data but keep legacy tables):
--   docker compose exec backend python -m migrations.migrate_2026_05_refactor
--
-- Finalize (also drop legacy tables / fields):
--   docker compose exec -e MIGRATION_DROP_LEGACY=1 backend \
--       python -m migrations.migrate_2026_05_refactor
--
-- This .surql file is a manual fallback for operators who want to drop legacy
-- structures by hand after a successful runner pass. The runner already does
-- everything below when MIGRATION_DROP_LEGACY=1.

REMOVE TABLE IF EXISTS notes;
REMOVE TABLE IF EXISTS parent_doc;
REMOVE TABLE IF EXISTS parent_ref;
REMOVE TABLE IF EXISTS mentions;
REMOVE TABLE IF EXISTS refs;
REMOVE TABLE IF EXISTS ref_embeddings;

REMOVE FIELD IF EXISTS reference_id ON chat_sessions;
REMOVE FIELD IF EXISTS context_reference_ids ON chat_sessions;
REMOVE INDEX IF EXISTS idx_chatsessions_user_ref ON chat_sessions;

REMOVE TABLE IF EXISTS _migration_ref_map;
REMOVE TABLE IF EXISTS _migration_note_map;
