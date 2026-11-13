-- 2026-05 Refactor — References → Documents-with-flag, Notes → Note-Chats
--
-- Drops the legacy tables / fields the refactor left behind. Run by hand, once per
-- environment, after checking INFO FOR DB; IRREVERSIBLE — take a dump first.
-- schema.surql declares none of these, and nothing reads or writes them.
-- Precondition: the legacy rows were copied into documents/chat_sessions by the
-- refactor's (now retired) runner — verify that before dropping; this file copies nothing.
--
-- INVARIANT(data-loss): this file ships DISARMED — the THROW below cancels the whole
-- transaction, so running it as-is removes nothing. To run it, delete the THROW line in
-- a local copy, never in the tree.
-- Why: it sits next to schema.surql and ships in the backend image; a mistaken import
-- of it would drop eight tables on any database it reaches, with no way back but a dump.

BEGIN TRANSACTION;

THROW "migrate_2026_05_refactor.surql is disarmed: delete this THROW line in a local copy to drop the legacy tables (irreversible)";

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

COMMIT TRANSACTION;
