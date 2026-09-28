-- Lore — SurrealDB Schema
-- Applied on backend startup (DEFINE ... IF NOT EXISTS is idempotent)

-- ─── Analyzers ────────────────────────────────────────────────────────────────
-- FTS analyzers (SurrealDB v3 FULLTEXT). snowball(russian) ONLY: a single analyzer
-- cannot chain two snowball filters (snowball(english), snowball(russian) double-stems
-- and corrupts both). Project is RU-primary; EN matches on surface form (the lowercased
-- token — snowball lowercases, so no separate `lowercase` filter). Title also gets
-- edgengram(3,10) for 3-char prefix matching; body omits it to avoid index bloat over
-- full document bodies. Consumed by search_documents (see search_documents in backend/routes/projects.py).
DEFINE ANALYZER IF NOT EXISTS lore_text_title
  TOKENIZERS class FILTERS snowball(russian), edgengram(3,10);
DEFINE ANALYZER IF NOT EXISTS lore_text_body
  TOKENIZERS class FILTERS snowball(russian);

-- ─── Users ────────────────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS users SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name          ON users TYPE string;
DEFINE FIELD IF NOT EXISTS email         ON users TYPE option<string>;
DEFINE FIELD OVERWRITE role          ON users TYPE string ASSERT $value IN ['admin', 'moderator', 'user'];
DEFINE FIELD IF NOT EXISTS password_hash ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS pin_hash      ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_facts    ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS timezone      ON users TYPE option<string>;
-- created_by: which admin created the account (the moderator's group is the
-- closure over "who created whom"). option<string>, no default — a user created
-- outside create_user_admin (self-registered / seed / migrations) carries NONE.
-- Named created_by, NOT invited_by: pending_invites.invited_by already means
-- "the project owner who invited this email" — a different relation.
DEFINE FIELD IF NOT EXISTS created_by    ON users TYPE option<string>;
-- moderator_id: the user's group — the uid of the role='moderator' row that manages
-- them. Mutable by admin only (PATCH splits MANAGER_FIELDS/ADMIN_FIELDS in
-- routes/users.py). Legal ONLY on role='user' rows and only pointing at a
-- role='moderator' row — enforced in the handler (422), not here: a cross-row
-- ASSERT is not expressible on a field. Promotion clears it (flat groups, no
-- nesting); demote/delete of a moderator with members is refused with 409.
DEFINE FIELD IF NOT EXISTS moderator_id ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at    ON users TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS token_version ON users TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS updated_at    ON users TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS deleted_at    ON users TYPE option<datetime>;
DEFINE INDEX IF NOT EXISTS idx_users_email ON users FIELDS email UNIQUE;

-- ─── App Meta (one-shot migration markers, e.g. sort_keys_backfill) ──────────
-- WHY: created lazily by startup migrations (main.py); must be declared so a SELECT
-- against it on a fresh DB doesn't fail. surrealdb 2.0.0 raises NotFoundError on a query
-- to an undefined table (1.0.4 silently returned empty), which broke startup backfill.
DEFINE TABLE IF NOT EXISTS app_meta SCHEMALESS;

-- ─── User Preferences (per-user-per-project UI state) ────────────────────────

-- ARCH: the mode is set with ALTER, not with the DEFINE below. Why: `DEFINE TABLE IF NOT
-- EXISTS` is a no-op on an existing table, so it freezes whatever mode the table had when
-- its DATABASE was first created — and environments drifted apart on exactly that.
-- Until 2026-07-28 prod and the main dev DB were SCHEMALESS (created before this file said
-- SCHEMAFULL) while every test DB was SCHEMAFULL (created after). The DDL text was
-- therefore true in one environment and false in the other, and the `FLEXIBLE` line below
-- failed on EVERY prod boot because FLEXIBLE is legal only on a SCHEMAFULL table.
-- ALTER is idempotent and unconditional, so the file is authoritative again.
DEFINE TABLE IF NOT EXISTS user_preferences SCHEMAFULL;
ALTER TABLE user_preferences SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS user_id     ON user_preferences TYPE string;
DEFINE FIELD IF NOT EXISTS project_id  ON user_preferences TYPE string;
-- INVARIANT: FLEXIBLE must survive any future edit to this line, and the line stays
-- OVERWRITE. Why: on a SCHEMAFULL table a plain `TYPE object` rejects every child it does
-- not declare, so a preferences save fails with "Found field 'preferences.<key>'" the
-- moment the UI adds a key. OVERWRITE rather than IF NOT EXISTS for the same reason the
-- table mode uses ALTER — an existing definition makes IF NOT EXISTS a no-op, so a field
-- once created without FLEXIBLE could never be repaired by this file.
DEFINE FIELD OVERWRITE preferences ON user_preferences TYPE object FLEXIBLE;
DEFINE FIELD IF NOT EXISTS updated_at  ON user_preferences TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_uprefs_unique ON user_preferences FIELDS user_id, project_id UNIQUE;

-- ─── Telemetry Events (append-only diagnostics from live clients) ────────────
-- ARCH: generic queryable telemetry channel. Producers: collab reconnects,
-- perf/lag metrics, agent turn errors, and agent-tool usage (the @track_agent_tool
-- decorator on the Tool-API). `detail` is FLEXIBLE so the channel stays reusable
-- without schema churn per event type. Append-only — NO soft-delete; bounded by
-- retention (see telemetry_retention_task, 90d), not by `deleted_at`.

DEFINE TABLE IF NOT EXISTS telemetry_event SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS category   ON telemetry_event TYPE string;          -- 'collab' | 'perf' | 'agent' | 'agent_tool'
DEFINE FIELD IF NOT EXISTS kind       ON telemetry_event TYPE string;          -- e.g. 'close','reconnect','lag','sync-slow'
DEFINE FIELD IF NOT EXISTS user_id    ON telemetry_event TYPE string;
DEFINE FIELD IF NOT EXISTS project_id ON telemetry_event TYPE option<string>;
DEFINE FIELD IF NOT EXISTS entity_id  ON telemetry_event TYPE option<string>;
DEFINE FIELD IF NOT EXISTS detail     ON telemetry_event TYPE object FLEXIBLE; -- code, reason, attempt, delay, ms, conn, ua, …
DEFINE FIELD IF NOT EXISTS client_ts  ON telemetry_event TYPE option<datetime>;-- browser Date (optional — server events omit it)
DEFINE FIELD IF NOT EXISTS created_at ON telemetry_event TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_telemetry_cat_time ON telemetry_event FIELDS category, created_at;
DEFINE INDEX IF NOT EXISTS idx_telemetry_user ON telemetry_event FIELDS user_id;

-- ─── Projects ─────────────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS projects SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name                 ON projects TYPE string;
DEFINE FIELD IF NOT EXISTS status               ON projects TYPE string DEFAULT 'active' ASSERT $value IN ['active', 'paused', 'done'];
DEFINE FIELD IF NOT EXISTS project_context      ON projects TYPE option<string>;
DEFINE FIELD IF NOT EXISTS index_doc_id         ON projects TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at           ON projects TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS last_accessed_doc_id ON projects TYPE option<string>;
DEFINE FIELD IF NOT EXISTS owner_id             ON projects TYPE option<string>;
DEFINE FIELD IF NOT EXISTS is_public            ON projects TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS voice_recording_doc_id ON projects TYPE option<string>;
-- Project-wide: references panel renders image refs as a gallery (true) or as plain cards.
DEFINE FIELD IF NOT EXISTS ref_image_preview     ON projects TYPE bool DEFAULT true;
-- INVARIANT(corruption): a non-option bool added to `projects` is backfilled here.
-- Why: DEFAULT fills only new rows; every existing row holds NONE, and the next
-- UPDATE on it (record_doc_open writing last_accessed_doc_id) fails coercion — the
-- user sees "Couldn't open this document" (same trap as the `documents` block below).
UPDATE projects SET ref_image_preview = true WHERE ref_image_preview IS NONE;
DEFINE FIELD IF NOT EXISTS deleted_at            ON projects TYPE option<datetime>;

-- ─── Project Members ──────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS project_members SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS project_id   ON project_members TYPE string;
DEFINE FIELD IF NOT EXISTS user_id      ON project_members TYPE string;
DEFINE FIELD OVERWRITE access_level ON project_members TYPE string ASSERT $value IN ['full', 'commentator', 'readonly'];
DEFINE FIELD IF NOT EXISTS last_accessed_doc_id ON project_members TYPE option<string>;
DEFINE INDEX IF NOT EXISTS idx_pm_project ON project_members FIELDS project_id;
DEFINE INDEX IF NOT EXISTS idx_pm_user    ON project_members FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_pm_unique  ON project_members FIELDS project_id, user_id UNIQUE;

-- document_access: DROPPED — a document inherits its project's access level.
-- The document_access_drop migration REMOVEs the table on DBs created before the
-- drop; it is not REMOVEd here because apply_schema runs BEFORE migrations and
-- the migration logs the row counts first.

-- ─── Document shares — anonymous read-only public links ─────────────────────
-- A share publishes ONE document (scope='doc') or its whole subtree
-- (scope='subtree') as an anonymous, read-only link. The plaintext token IS
-- the URL segment (/s/:token) and is stored verbatim so the owner can re-copy /
-- re-send the link at any time. The public-share router resolves the token →
-- (project_id, root, scope) and derives the readable doc set via
-- scope.subtree_doc_ids (depth-capped 50 — see db.py).
--
-- INVARIANT(security): the public router is READ-ONLY by construction — no
-- write surface exists on /api/public/*. "Доступ для всех только чтение".
--
-- DECISION-PIN (plan "iridescent-wibbling-heron", override): the plaintext
-- token is stored, NOT hashed. The original plan stored only sha256(token) with
-- a "shown once" UX (mirroring api_keys). That model is security theater for a
-- PUBLIC link: the content is by design readable by ANYONE holding the URL, so
-- the URL is inherently leaky (browser history, proxy logs, Referer); and a DB
-- leak exposes the content directly — hashing the link protects nothing real
-- while breaking the legitimate "re-send the link anytime" UX. Contrast with
-- api_keys, where the token IS a secret credential (identity-bound, grants
-- writes) and "shown once + hash-only" is load-bearing. Why pinned: prevent a
-- future reflexive "hash it for safety" revert that would re-break the UX
-- without adding meaningful protection.

DEFINE TABLE IF NOT EXISTS document_shares SCHEMAFULL;
-- Drop the legacy hashed-token column (DECISION-PIN: token now stored plaintext).
-- REMOVE FIELD IF EXISTS is idempotent — safe on fresh AND stale (pre-overturn) DBs.
REMOVE FIELD IF EXISTS token_hash ON document_shares;
DEFINE FIELD IF NOT EXISTS project_id  ON document_shares TYPE string;
DEFINE FIELD IF NOT EXISTS document_id ON document_shares TYPE string;
-- token: the plaintext public-link token (URL segment /s/:token). Stored
-- verbatim — see DECISION-PIN above for why hashing is not applied here.
DEFINE FIELD IF NOT EXISTS token       ON document_shares TYPE string;
DEFINE FIELD OVERWRITE     scope       ON document_shares TYPE string ASSERT $value IN ['doc','subtree'];
DEFINE FIELD IF NOT EXISTS created_at  ON document_shares TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_by  ON document_shares TYPE option<string>;
DEFINE FIELD IF NOT EXISTS deleted_at  ON document_shares TYPE option<datetime>;
DEFINE INDEX IF NOT EXISTS idx_dshare_doc     ON document_shares FIELDS document_id;
DEFINE INDEX IF NOT EXISTS idx_dshare_token   ON document_shares FIELDS token;
DEFINE INDEX IF NOT EXISTS idx_dshare_project ON document_shares FIELDS project_id;

-- ─── Pending invites (silent-invite queue) ───────────────────────────────────
-- Email-keyed project grant queued before user registration. Flushed at user
-- creation (see routes/users.py create_user_admin).

DEFINE TABLE IF NOT EXISTS pending_invites SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS email        ON pending_invites TYPE string;
DEFINE FIELD IF NOT EXISTS project_id   ON pending_invites TYPE string;
DEFINE FIELD OVERWRITE access_level ON pending_invites TYPE string ASSERT $value IN ['full', 'commentator', 'readonly'];
DEFINE FIELD IF NOT EXISTS invited_by   ON pending_invites TYPE string;
DEFINE FIELD IF NOT EXISTS created_at   ON pending_invites TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_pi_email  ON pending_invites FIELDS email;
-- The document_access_drop migration redefines this index on DBs that carry
-- the older (email, project_id, document_id) shape.
DEFINE INDEX IF NOT EXISTS idx_pi_unique ON pending_invites FIELDS email, project_id UNIQUE;

-- ─── Registration invites (invite-link tokens) ────────────────────────────────
-- Single-use, TTL-bounded tokens minted by admin|moderator for the /register/<token>
-- page (routes/invites.py). token_hash is
-- sha256 (never plaintext — same posture as api_keys); moderator_id pre-binds the
-- registered account to the minting moderator's group. Distinct from
-- pending_invites (email → project grant, flushed at user creation).

DEFINE TABLE IF NOT EXISTS registration_invites SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS token_hash    ON registration_invites TYPE string;
DEFINE FIELD IF NOT EXISTS invited_by    ON registration_invites TYPE string;
DEFINE FIELD IF NOT EXISTS moderator_id  ON registration_invites TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at    ON registration_invites TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at    ON registration_invites TYPE datetime;
DEFINE FIELD IF NOT EXISTS used_at       ON registration_invites TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS used_by       ON registration_invites TYPE option<string>;
DEFINE INDEX IF NOT EXISTS idx_reginv_token ON registration_invites FIELDS token_hash UNIQUE;

-- ─── Documents ────────────────────────────────────────────────────────────────

-- ARCH: mode set with ALTER — same drift and same reasoning as `user_preferences` above
-- (prod was SCHEMALESS, every test DB SCHEMAFULL, and `IF NOT EXISTS` could never
-- reconcile them). See that block.
DEFINE TABLE IF NOT EXISTS documents SCHEMAFULL;
ALTER TABLE documents SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS project_id ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS parent_id  ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS title      ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS content    ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS path       ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS is_index   ON documents TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON documents TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at  ON documents TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS last_editor_id   ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS last_editor_name ON documents TYPE option<string>;
-- Reference author attribution: the identity + denormalized nickname of the user who
-- CREATED a reference, so the RefCard meta row can show the creator's nick for someone
-- else's reference. Distinct from last_editor_*
-- (which flips to whoever edited last). Written ONLY by the reference creation chokepoint
-- (documents_service.create_reference_row); non-reference documents leave it unset.
-- WHY denormalized name (not a join on users): the reference panel lists tens of rows and
-- resolving each created_by against `users` would be an N+1 on every LIST. A later user
-- rename leaves a stale nickname on old references — the same accepted tradeoff as
-- last_editor_name. option<string> with NO non-option DEFAULT, so a row predating it reads
-- NONE = "no author" (impersonal/legacy paths) — the UI renders nothing for it
-- (no "System"/"Unknown" label). Deliberately NOT backfilled: a table-wide
-- `UPDATE documents` re-validates each record against SCHEMAFULL and dies on legacy rows
-- carrying pre-SCHEMAFULL fields (see the archived comment at :209-225).
DEFINE FIELD IF NOT EXISTS created_by      ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_by_name ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS deleted_at       ON documents TYPE option<datetime>;
-- INVARIANT: sort_key is option<string> (NOT a non-option DEFAULT). Why: a
-- non-option DEFAULT only applies on CREATE; pre-existing rows keep NONE and
-- every later UPDATE coerce-fails silently (flush_to_db swallows it). option<>
-- avoids that trap; existing rows are backfilled by main.py::_backfill_sort_keys.
DEFINE FIELD IF NOT EXISTS sort_key   ON documents TYPE option<string>;
-- ─── Phase-3: config & knowledge as documents (plan stage-1 §3.4) ───────────
-- is_system marks the reserved agent-brain subtree. option<bool> DEFAULT false:
-- option-typed (NOT a non-option DEFAULT) to avoid the silent-write trap noted
-- for sort_key above; existing rows backfill to false via the runner migration.
DEFINE FIELD IF NOT EXISTS is_system   ON documents TYPE option<bool> DEFAULT false;
-- system_role names the doc's place in the reserved subtree: the always-injected
-- skeleton (system_root | system_prompt (Personas folder) | rules_folder |
-- skills_folder | knowledge_folder) plus the seeded-but-never-injected Memory
-- folder (memory_folder). The legacy leaf roles
-- (persona/rules/skill/knowledge) were cleared to NONE by a one-time migration
-- (since retired with the applied past) — personas are positional (direct children
-- of the Personas folder) and folder children are plain docs (system_role = NONE).
-- `memory_persona` is the one legacy value still present in the data: a one-time
-- retirement migration TOMBSTONED those rows and KEPT the tag (it was that
-- migration's WHERE key, and the row had to stay recoverable — see its
-- INVARIANT(tombstone-not-drop)). So it is never LIVE, but a `system_role NOT IN
-- <vocabulary>` sweep would still hit it. The Tools/Comfy roles (tools_folder |
-- comfy | comfy_prompt | comfy_workflow | comfy_settings) sit in the same state:
-- the Comfy config became instance admin settings and a one-time migration
-- tombstoned those rows with the tag kept. Only meaningful on is_system docs.
DEFINE FIELD IF NOT EXISTS system_role ON documents TYPE option<string>;
-- ARCH (plan reference-archive-v2): `archived` is the staged-delete (archive) flag for
-- references. option<bool> DEFAULT false (NOT a non-option DEFAULT) to avoid the
-- NONE-coerce-on-UPDATE silent-write trap noted for sort_key above (:192-201) and the
-- is_system pattern (:201). Rows predating the field keep NONE and are NOT backfilled:
-- every consumer reads NONE as "live" (`archived != true` in the LIST filter,
-- `_archived_sink_key` in the sort), so a sweep buys nothing. A backfill migration was
-- written and removed — a table-wide `UPDATE documents` re-validates each record against
-- SCHEMAFULL and dies on legacy rows carrying pre-SCHEMAFULL fields ("Found field
-- 'position', but no such field exists"), which crash-looped the backend at startup.
DEFINE FIELD IF NOT EXISTS archived     ON documents TYPE option<bool> DEFAULT false;

-- ─── Project memory (plan project-memory-mvp §1 / design record P8, P12) ────
-- A FACT doc: one document per distilled fact (Order 2 — the entity level was
-- deleted; there is one identity level, not two). A fact lives in the project's
-- single reserved `Memory` folder. Its `content` IS the fact (authored prose, never
-- a projection), and `mem` carries the fact's metadata (provenance, retirement,
-- version history). Both memory flags are option<...> with NO non-option DEFAULT —
-- a row predating them reads NONE, which every consumer reads as "not a memory
-- doc". Deliberately NOT backfilled: a table-wide `UPDATE documents` re-validates
-- each row against SCHEMAFULL and dies on legacy rows carrying pre-SCHEMAFULL
-- fields, which crash-looped the backend at startup (see the `archived` comment
-- above — that backfill was written and removed).
DEFINE FIELD IF NOT EXISTS is_memory       ON documents TYPE option<bool> DEFAULT false;
-- `mem_active` labels a LIVE fact vs a RETIRED one. Top-level (not inside `mem`)
-- because memory/stats.py reads it as the label and idx_documents_memory indexes it
-- as the "retired facts still in this project" sweep axis.
-- INVARIANT: retirement SOFT-DELETES (`_retire_fact` sets `deleted_at` in the same
-- UPDATE as `mem_active = false`) — `deleted_at` is the ONE invisibility axis every
-- reader filters on; `mem_active` never hides a fact, it only tells a merge-retirement
-- apart from a user deletion.
-- Why: two invisibility axes on one table are a defect class — readers filtered
-- `deleted_at` project-wide and kept forgetting the memory-only `mem_active` clause,
-- which leaked retired facts into search_materials AND _allowed_fact_ids.
-- INVARIANT: `mem_active` has THREE states and therefore NO `DEFAULT false` — unlike
-- `is_memory` one line above. NONE = "not a memory doc at all"; true = a live fact;
-- false = a retired fact (superseded or merged-away, kept for link stability).
-- Why: the asymmetry with `is_memory` reads as an oversight, and "fixing" it to
-- `DEFAULT false` would stamp every ordinary document in the project as a retired
-- memory fact — they would all enter idx_documents_memory, and the "retired facts
-- still in this project" sweep this index exists for would return the whole
-- document table. A fact is born `true` at apply time (`_create_fact`); retirement
-- is the only thing that sets it `false`.
DEFINE FIELD IF NOT EXISTS mem_active      ON documents TYPE option<bool>;
-- The fact's metadata: provenance (the references it came from — D8 recoverability),
-- survivor-selection fields, the retirement triple, and the version history the
-- on-request `get_claim_history` channel serves. FLEXIBLE (like file_meta) because a
-- SCHEMAFULL object requires every nested field declared and the whole UPDATE fails
-- on an undeclared one, and the history chain is a list of objects whose shape this
-- module owns and the schema should not freeze.
-- INVARIANT: a fact's body (`content`) IS the fact — there is no second content
-- model inside `mem`. Order 2 deleted `mem.claims[]` (the entity-level source of
-- truth) and the render projection that turned it into a body; the body is now
-- authored prose, so an apply never re-derives it.
DEFINE FIELD IF NOT EXISTS mem             ON documents TYPE option<object> FLEXIBLE;
-- The consolidation cursor (plan 1784300000000-memory-incremental-runs): the stamp
-- that makes a run incremental WITHOUT run state (ARCH P11 — deliberately NO runs
-- table). `mem_consolidated_at` = when a run consumed the reference;
-- `mem_consolidated_run` = which run consumed it (provenance symmetry with
-- `claim.run_id`). Both are option<...> with NO non-option DEFAULT, so a row
-- predating them reads NONE = "not yet consumed" (exactly like is_memory/archived
-- above — a backfill re-validates every row against SCHEMAFULL and crash-loops the
-- backend, see the archived comment at :209-217).
-- INVARIANT: a stamp is written ONLY on reference rows (is_reference=true); a stamp
-- on a non-reference is meaningless and is never written (enforced at
-- backend/memory/task_builder.py `next_reference`).
-- Why: the cursor is what keeps "resume is the absence of a feature" true — a run
-- that dies mid-way leaves its references unstamped, so the next
-- `consolidate_memory` simply continues; and an upload added next month is the only
-- thing a later run reads. Writing the stamp when the AGENT says it is done with a
-- reference (next_reference), not on apply, is the accepted crash window: a stamp
-- never precedes the apply that justifies it.
DEFINE FIELD IF NOT EXISTS mem_consolidated_at  ON documents TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS mem_consolidated_run ON documents TYPE option<string>;
-- The cursor that walks INSIDE a reference: how many WINDOWS of it the current run has
-- consumed. `content` is sliced on the way OUT (backend/memory/_window.slice_windows),
-- and this field records progress so a multi-window reference is served one window per
-- portion.
-- `mem_consolidated_at` is written only when the LAST window completes; until then each
-- `next_reference` advances `mem_window_done` and serves the next window of the SAME row.
-- option<int>, NO default ⇒ a row predating it reads NONE = "no windows consumed yet"
-- (exactly like the two fields above — a backfill re-validates against SCHEMAFULL and
-- crash-loops the backend the same way, see the archived comment at :209-217).
-- INVARIANT: cleared to NONE TOGETHER with the two stamp fields by EVERY writer of the
-- run cursor — _clear_consumed_stamps, reopen_consolidation, and files_service's
-- reprocess (which also replaces `content`). Why: a window cursor that survives a
-- clear silently drops material — the reference resumes at window 3 and never re-reads
-- windows 1-2 — so all three cursor fields clear as one (tested as the FIELD, not a
-- "looks fine" re-run). Written ONLY on reference rows (is_reference=true), alongside
-- the stamp; a value on a non-reference is meaningless. Resumed by absence: NONE means
-- window 0 is next, identical to a freshly-seeded reference.
DEFINE FIELD IF NOT EXISTS mem_window_done    ON documents TYPE option<int>;

DEFINE INDEX IF NOT EXISTS idx_documents_path ON documents FIELDS project_id, path UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_documents_project_deleted ON documents FIELDS project_id, deleted_at;
DEFINE INDEX IF NOT EXISTS idx_documents_sibling_order ON documents FIELDS project_id, parent_id, sort_key;
DEFINE INDEX IF NOT EXISTS idx_documents_system ON documents FIELDS project_id, is_system, system_role;
-- "retired facts still in this project" — the sweep axis. There is ONE memory space
-- per project (design P12), so no scope column participates.
DEFINE INDEX IF NOT EXISTS idx_documents_memory ON documents FIELDS project_id, is_memory, mem_active;
-- FTS: one index per field (a FULLTEXT index is strictly ONE field). The cross-index
-- OR in search_documents pairs each field to its index via the numbered match operator
-- (title @0@ $q OR content @1@ $q). No BM25/HIGHLIGHTS: ranking stays Python match_count
-- (see search_documents in backend/routes/projects.py), so these stay lean.
DEFINE INDEX IF NOT EXISTS idx_documents_fts_title
  ON documents FIELDS title   FULLTEXT ANALYZER lore_text_title CONCURRENTLY;
DEFINE INDEX IF NOT EXISTS idx_documents_fts_content
  ON documents FIELDS content FULLTEXT ANALYZER lore_text_body  CONCURRENTLY;


-- ─── Checkpoints ──────────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS checkpoints SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS document_id  ON checkpoints TYPE string;
-- option<string> is correct for fresh DBs; existing DBs created with `TYPE string`
-- need DEFINE FIELD OVERWRITE (IF NOT EXISTS won't redefine) — see
-- migrations/migrate_cp_content_nullout.py before nulling inline content.
DEFINE FIELD IF NOT EXISTS content      ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS content_ref  ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS label        ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS comment      ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS content_hash ON checkpoints TYPE option<string>;
-- INVARIANT: tables_json + tables_hash capture the FULL table state (getMap('tables')):
-- ids, column widths and cell text, so a restore rebuilds tables identically. Stored
-- INLINE (not in a blob) — tables are small relative to document text and inline keeps
-- restore a single read. Why: snapshots previously dropped the tables Yjs subtree, so a
-- restore showed flat GFM or orphan-anchor errors instead of an editable table block.
DEFINE FIELD IF NOT EXISTS tables_json  ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS tables_hash  ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_by   ON checkpoints TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at   ON checkpoints TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS deleted_at   ON checkpoints TYPE option<datetime>;

DEFINE INDEX IF NOT EXISTS idx_checkpoints_doc ON checkpoints FIELDS document_id;
DEFINE INDEX IF NOT EXISTS idx_checkpoints_doc_deleted ON checkpoints FIELDS document_id, deleted_at;
-- Index-serves the backfill/nullout predicates `content_ref IS NONE` and
-- `content IS NOT NONE AND content_ref IS NONE` (jobs/tasks.backfill_cp_blobs_task,
-- nullout_inline_content_task, migrate_cp_content_nullout). Why: those run on recurring
-- crons against an unboundedly-growing table; without this index each pass full-scans
-- checkpoints even after convergence (0 matching rows).
DEFINE INDEX IF NOT EXISTS idx_checkpoints_content_ref ON checkpoints FIELDS content_ref;

-- ─── Document History (creation + first-edit-per-user log) ──────────────────
-- INVARIANT: one `edited` row per (document_id, user_id), enforced by the unique
-- index, not application code. Why: a SELECT-then-INSERT check is a check-then-act
-- race across replicas/multiplexed sessions; the DB-level unique constraint is the
-- only correct gate. log_first_edit attempts the insert and treats a unique-index
-- violation as a no-op (idempotent).

DEFINE TABLE IF NOT EXISTS document_history SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS document_id ON document_history TYPE string;
DEFINE FIELD IF NOT EXISTS user_id      ON document_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_name    ON document_history TYPE string;
DEFINE FIELD IF NOT EXISTS action       ON document_history TYPE string
    ASSERT $value IN ['created', 'edited'];
DEFINE FIELD IF NOT EXISTS created_at   ON document_history TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_dh_doc ON document_history FIELDS document_id;
DEFINE INDEX IF NOT EXISTS idx_dh_doc_user_action ON document_history FIELDS document_id, user_id, action UNIQUE;

-- ─── Checkpoint Blobs (content-addressable, immutable, NEVER deleted) ──────────
-- ARCH: Two-layer storage — thin checkpoint rows reference immutable compressed
-- blobs. Dedup is global (id = sha256 of the original content). Blobs are never
-- deleted; only AUTO checkpoint rows may be thinned (soft-delete). Phase 0
-- diagnostic showed dedup+zstd alone achieves 82.9% reduction (3.8MB→668KB) —
-- delta chains are not needed (kill criterion met).
-- INVARIANT: blobs are immutable and never deleted; identical content is stored
-- once (id = sha256 of the full content, never of a diff). Why: data layer must
-- preserve every version forever (user rule); dedup keyed on full-content hash
-- bounds growth.

DEFINE TABLE IF NOT EXISTS cp_blobs SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS kind       ON cp_blobs TYPE string DEFAULT 'full'
    ASSERT $value IN ['full', 'delta'];
DEFINE FIELD IF NOT EXISTS base_ref   ON cp_blobs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS depth      ON cp_blobs TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS data       ON cp_blobs TYPE bytes;
DEFINE FIELD IF NOT EXISTS size       ON cp_blobs TYPE int;
DEFINE FIELD IF NOT EXISTS created_at ON cp_blobs TYPE datetime DEFAULT time::now();

-- Edge: document links to a document (content reference, including ref-docs)
-- WHY: TYPE RELATION required for RELATE statements in SurrealDB v3+. Without
-- it RELATE fails with "Found record: ... which is a relation, but expected a
-- NORMAL". Existing legacy DBs created this as a NORMAL SCHEMALESS table and
-- silently accepted RELATE — fresh DBs (CI) reject it.
DEFINE TABLE IF NOT EXISTS doc_mentions TYPE RELATION SCHEMAFULL;
DEFINE FIELD OVERWRITE in  ON doc_mentions TYPE record<documents>;
DEFINE FIELD IF NOT EXISTS out ON doc_mentions TYPE record<documents>;

-- ─── API Keys ────────────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS api_keys SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS user_id      ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS project_id   ON api_keys TYPE string;
-- document_id is the universal scope root (one field, three cases):
--   - widget capability: the single document the key is bound to (always set);
--   - agent capability, user-issued: the subtree root the key is sandboxed to
--     (= the doc it was issued from; plan "glimmering-knitting-pebble");
--   - agent capability, internal (Pi driver): '' = whole project.
DEFINE FIELD IF NOT EXISTS document_id  ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS token_hash   ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS label        ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS created_at   ON api_keys TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS last_used_at ON api_keys TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS expires_at   ON api_keys TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS deleted_at   ON api_keys TYPE option<datetime>;
-- capabilities (plan "glimmering-knitting-pebble"): ONE key row carries a
-- non-empty subset of {widget, agent} — which auth surfaces accept the token.
-- Replaces key_kind (removed by migration api_keys_drop_key_kind; pre-release,
-- no legacy rows to backfill). ALLINSIDE, not IN — IN has
-- different (membership) semantics for array operands.
DEFINE FIELD OVERWRITE capabilities ON api_keys TYPE array<string>
	ASSERT array::len($value) > 0 AND $value ALLINSIDE ['widget', 'agent'];
-- internal (same plan): marks keys minted by the Pi-driver path — excluded from
-- every user-facing list and from the doc-tree indicator. Explicit flag, NOT a
-- label heuristic: label is pure display.
DEFINE FIELD IF NOT EXISTS internal ON api_keys TYPE bool DEFAULT false;
-- auto_apply (plan mcp-gateway-ergonomics-v2 Slice 3): a per-key grant that lets
-- the MCP gateway auto-apply writes (loop-closing autonomy). `bool DEFAULT false`
-- (NOT option<bool>): the field MUST always be a defined bool — the grant is a real
-- per-key decision read on every mutating call. A migration failure that left a
-- granted key's row reading None would silently neutralize its grant (the gateway
-- treats only an explicit True as granted) and that is hard to diagnose. crash-on-
-- missing-config: do NOT null-coalesce over an option in code — that hides a
-- migration that did not take. Live-verified: a pre-migration key reads `false`.
DEFINE FIELD IF NOT EXISTS auto_apply   ON api_keys TYPE bool DEFAULT false;
-- chat_session_id: the discriminator of the per-chat-session STANDING agent
-- key (the driver's tool credential) — one api_keys row per chat session,
-- minted on first use, revoked on session delete and on access-level change.
-- NONE on every other key kind (run keys, user-issued keys); label is pure
-- display and NEVER a discriminator, so no other key kind can adopt a
-- session row and the session lookup can adopt no other kind.
DEFINE FIELD IF NOT EXISTS chat_session_id ON api_keys TYPE option<string>;

DEFINE INDEX IF NOT EXISTS idx_apikeys_user ON api_keys FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_apikeys_doc  ON api_keys FIELDS document_id;
DEFINE INDEX IF NOT EXISTS idx_apikeys_token_hash ON api_keys FIELDS token_hash;
DEFINE INDEX IF NOT EXISTS idx_apikeys_chat_session ON api_keys FIELDS chat_session_id;

-- ─── Chat Sessions ────────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS chat_sessions SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS project_id       ON chat_sessions TYPE string;
DEFINE FIELD IF NOT EXISTS document_id      ON chat_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_id          ON chat_sessions TYPE string;
DEFINE FIELD IF NOT EXISTS title            ON chat_sessions TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS model            ON chat_sessions TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS system_prompt_id    ON chat_sessions TYPE option<string>;
-- Per-session reasoning effort (plan reasoning-effort-selector): option<string>,
-- NOT string DEFAULT '' like `model` — null must be representable (null = Default,
-- the harness materializes the adapter default). Validated at PATCH time against
-- the target model's advertised list (models_catalog.advertised_effort_levels).
DEFINE FIELD IF NOT EXISTS reasoning_effort    ON chat_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS context_document_ids  ON chat_sessions TYPE option<array<string>>;
DEFINE FIELD IF NOT EXISTS context_document_ids[*] ON chat_sessions TYPE string;
-- INVARIANT: legacy reference_id/context_reference_ids columns retired 2026-05.
-- Why: folded into document_id/context_document_ids by migrate_2026_05_refactor.merge_chat_session_refs.
-- (API param reference_id is preserved — see models.py AliasChoices.)
DEFINE FIELD IF NOT EXISTS created_at       ON chat_sessions TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at       ON chat_sessions TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS deleted_at       ON chat_sessions TYPE option<datetime>;

DEFINE INDEX IF NOT EXISTS idx_chatsessions_user_project ON chat_sessions FIELDS user_id, project_id;
DEFINE INDEX IF NOT EXISTS idx_chatsessions_user_document ON chat_sessions FIELDS user_id, project_id, document_id;
-- INVARIANT: document_id-leading index for the cascade-delete scan
-- (`chat_sessions WHERE document_id = $did`). Why: the two indexes above lead with
-- user_id, so a document_id-only predicate can't use them → full-table TableScan on
-- document delete. See cascade.py phase-1 reads.
DEFINE INDEX IF NOT EXISTS idx_chatsessions_document ON chat_sessions FIELDS document_id, deleted_at;

-- ARCH: Tree via parent_id — fork = new message with same parent_id (siblings).
-- Active path computed client-side via selectedSiblings map.
DEFINE TABLE IF NOT EXISTS messages SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS chat_id    ON messages TYPE string;
DEFINE FIELD IF NOT EXISTS parent_id  ON messages TYPE option<string>;
DEFINE FIELD IF NOT EXISTS role       ON messages TYPE string ASSERT $value IN ['user', 'assistant'];
DEFINE FIELD IF NOT EXISTS content    ON messages TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS images     ON messages TYPE option<array>;
DEFINE FIELD IF NOT EXISTS model      ON messages TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON messages TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS deleted_at ON messages TYPE option<datetime>;
-- ARCH (plan collapse-agent-stack-onto-dsh-vocabulary step 4): the entry_id
-- field + its chat/entry index are RETIRED with the canonical seam — at that
-- step the fork branch point became the projection-derived turn ordinal
-- (superseded again by messages.driver_seq, plan
-- collapse-the-editor-harness-layer step 5), and no canonical join key is
-- stamped or read anymore. REMOVE, not just a dropped DEFINE: a DB created
-- before the deletion keeps both, and the index would go on being maintained
-- over a field nothing writes.
REMOVE INDEX IF EXISTS idx_messages_chat_entry ON messages;
REMOVE FIELD IF EXISTS entry_id ON messages;
DEFINE INDEX IF NOT EXISTS idx_messages_chat ON messages FIELDS chat_id;
DEFINE INDEX IF NOT EXISTS idx_messages_chat_deleted ON messages FIELDS chat_id, deleted_at;

-- ─── Agent Configs ──────────────────────────────────────────────────────────

DEFINE TABLE IF NOT EXISTS agent_configs SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS document_id    ON agent_configs TYPE string;
DEFINE FIELD IF NOT EXISTS config_doc_id  ON agent_configs TYPE string;
DEFINE FIELD IF NOT EXISTS target_doc_id  ON agent_configs TYPE string;
DEFINE FIELD IF NOT EXISTS project_id     ON agent_configs TYPE string;
-- option<string> + OVERWRITE (not bare string): pre-existing rows — including
-- soft-deleted ones the backfill skips (deleted_at IS NONE filter) — carry NONE
-- here, and a non-option string traps them in the row-wide coerce on any UPDATE
-- (e.g. _migrate_agent_configs_kind_backfill). Same trap as agent_auto.
DEFINE FIELD OVERWRITE trigger_event  ON agent_configs TYPE option<string> DEFAULT "transcription_complete";
-- kind discriminates the two coexisting config families (GAP-7): "extractor"
-- (event-driven extractor jobs, the original use) vs "conversational" (the home
-- agent / Pi). Defaults to extractor so existing rows are never repurposed.
DEFINE FIELD IF NOT EXISTS kind          ON agent_configs TYPE option<string> DEFAULT "extractor";
-- role names a conversational config's doc role (e.g. system_prompt/rules/skill).
DEFINE FIELD IF NOT EXISTS role          ON agent_configs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS title_template ON agent_configs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS model          ON agent_configs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at     ON agent_configs TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS deleted_at     ON agent_configs TYPE option<datetime>;
DEFINE INDEX IF NOT EXISTS idx_agent_configs_doc_trigger ON agent_configs FIELDS document_id, trigger_event;
DEFINE INDEX IF NOT EXISTS idx_agent_configs_project  ON agent_configs FIELDS project_id;

-- MIGRATION: drop the old unique-on-document_id index (replaced by idx_agent_configs_doc_trigger)
REMOVE INDEX IF EXISTS idx_agent_configs_document ON agent_configs;

-- MIGRATION: backfill trigger_event on records created before the field existed
UPDATE agent_configs SET trigger_event = "transcription_complete" WHERE trigger_event IS NONE AND deleted_at IS NONE;

-- ─── Pipeline schedules (plan 1786740208300 S3, direction C) ─────────────────
-- A user-configurable schedule is PROJECT DATA (D7): the worker keeps ONE cron
-- tick (pipeline_schedule_tick_task) that claims and runs due entries, so adding
-- a scheduled pipeline never touches WorkerSettings. The four hardcoded
-- maintenance cron_jobs are deliberately untouched.
DEFINE TABLE IF NOT EXISTS pipeline_schedules SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS project_id   ON pipeline_schedules TYPE string;
-- `pipeline` is a REGISTRY KEY (pipeline/core/registry.py), validated against
-- pipeline_names() at create time; the tick re-resolves it and records
-- last_status='error' if the registry dropped it since.
DEFINE FIELD IF NOT EXISTS pipeline     ON pipeline_schedules TYPE string;
-- Minimum seconds between runs. int (not a cron expression): v1 covers
-- every-N-minutes/hours/days; the tick's own cadence is the resolution floor.
DEFINE FIELD IF NOT EXISTS interval_s   ON pipeline_schedules TYPE int;
DEFINE FIELD IF NOT EXISTS enabled      ON pipeline_schedules TYPE bool DEFAULT true;
-- params_json: the frozen TASK KWARGS (JSON string —
-- SCHEMAFULL would otherwise demand per-key FIELDs for free-form task args).
-- Dispatched verbatim as **kwargs onto the registry row's arq task.
DEFINE FIELD IF NOT EXISTS params_json  ON pipeline_schedules TYPE string;
DEFINE FIELD IF NOT EXISTS created_by   ON pipeline_schedules TYPE string;
-- Tick bookkeeping: last_run_at is set AT CLAIM (the CAS-by-predicate UPDATE that
-- makes two concurrent ticks dispatch exactly once); last_status is the DISPATCH
-- outcome ('ok' = enqueued; 'error' + last_error = claim/lookup/enqueue failure).
-- The pipeline's own run outcome is per-pipeline territory (the extractor
-- dead-letters its own error notes).
DEFINE FIELD IF NOT EXISTS last_run_at  ON pipeline_schedules TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_status  ON pipeline_schedules TYPE option<string>;
DEFINE FIELD IF NOT EXISTS last_error   ON pipeline_schedules TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at   ON pipeline_schedules TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at   ON pipeline_schedules TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS deleted_at   ON pipeline_schedules TYPE option<datetime>;
DEFINE INDEX IF NOT EXISTS idx_psched_project ON pipeline_schedules FIELDS project_id;
DEFINE INDEX IF NOT EXISTS idx_psched_enabled ON pipeline_schedules FIELDS enabled, deleted_at;

-- ─── Tool-API proposals ───────────────────────────────────────────────────────
-- tool_proposals: DROPPED (plan retire-the-proposal-card-render-cluster) — the
-- render tail that read it is deleted and nothing writes it; a one-time
-- migration (since retired with the applied past) REMOVEd the table, its rows
-- and the three indexes on DBs created before the drop.

-- ─── Embedding tables (RAG context for AI chat) ────────────────────────────────

DEFINE TABLE IF NOT EXISTS doc_chunks SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS document_id     ON doc_chunks TYPE string;
DEFINE FIELD IF NOT EXISTS project_id      ON doc_chunks TYPE string;
DEFINE FIELD IF NOT EXISTS ord             ON doc_chunks TYPE int;
DEFINE FIELD IF NOT EXISTS heading         ON doc_chunks TYPE option<string>;
DEFINE FIELD IF NOT EXISTS content         ON doc_chunks TYPE string;
DEFINE FIELD IF NOT EXISTS offset_start    ON doc_chunks TYPE int;
DEFINE FIELD IF NOT EXISTS offset_end      ON doc_chunks TYPE int;
DEFINE FIELD IF NOT EXISTS content_version ON doc_chunks TYPE int;
DEFINE FIELD IF NOT EXISTS content_hash    ON doc_chunks TYPE option<string>;
DEFINE FIELD IF NOT EXISTS embedding       ON doc_chunks TYPE array<float>;
-- kind: which retrieval corpus a chunk belongs to — 'memory' when the parent is
-- a fact (is_memory), else 'reference' (is_reference), else 'document'; the same
-- label rule the direct-hit layer applies (agent/search_exec.py). Stamped at
-- EVERY chunk write (embeddings._chunk_write_stmts, the UPDATE-reuse AND the
-- CREATE arm) so per-kind retrieval can filter BEFORE its LIMIT instead of
-- discarding wrong kinds after it. option<string>: rows predating the column
-- read NONE until the WEB's boot backfill fills them from the parent flags
-- (embedding_coverage.backfill_doc_chunk_kind) — the worker never backfills.
-- INVARIANT: kind is fixed at write (no code path flips is_reference/is_memory
-- after creation); a future reclassification path must emit content_flushed so
-- the re-embed restamps it. Why: retrieval filters on kind server-side, so a
-- chunk whose label no longer matches its parent silently vanishes from that
-- corpus's results.
DEFINE FIELD IF NOT EXISTS kind            ON doc_chunks TYPE option<string>;
-- model: the EMBEDDING_MODEL name that produced `embedding`. Deliberately NOT
-- backfilled: the stored vector's provenance cannot be proven for pre-column
-- rows (a matching content_hash only recomputes under the CURRENT model), so
-- NONE means "unknown, pre-column" and stays NONE until the document's next
-- re-embed stamps it; retrieval includes NONE-model rows, coverage counts them
-- stale (embedding_coverage).
DEFINE FIELD IF NOT EXISTS model           ON doc_chunks TYPE option<string>;
DEFINE FIELD IF NOT EXISTS updated_at      ON doc_chunks TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS doc_chunks_doc_idx  ON doc_chunks FIELDS document_id;
DEFINE INDEX IF NOT EXISTS doc_chunks_proj_idx ON doc_chunks FIELDS project_id;

-- messages: add sources field (denormalized retrieval results)
DEFINE FIELD IF NOT EXISTS sources ON messages TYPE option<array>;
DEFINE FIELD IF NOT EXISTS sources[*].kind         ON messages TYPE string;
DEFINE FIELD IF NOT EXISTS sources[*].id           ON messages TYPE string;
DEFINE FIELD IF NOT EXISTS sources[*].title        ON messages TYPE string;
DEFINE FIELD IF NOT EXISTS sources[*].heading      ON messages TYPE option<string>;
DEFINE FIELD IF NOT EXISTS sources[*].snippet      ON messages TYPE option<string>;
DEFINE FIELD IF NOT EXISTS sources[*].offset_start ON messages TYPE option<int>;
DEFINE FIELD IF NOT EXISTS sources[*].offset_end   ON messages TYPE option<int>;
DEFINE FIELD IF NOT EXISTS sources[*].score        ON messages TYPE option<float>;

-- documents: content_version for CAS-based embedding updates
DEFINE FIELD IF NOT EXISTS content_version ON documents TYPE int DEFAULT 0;

-- documents: embedding retry tracking
DEFINE FIELD IF NOT EXISTS embedding_status ON documents TYPE string DEFAULT 'ok';
-- WHY option<string> (was option<datetime>) + OVERWRITE (not IF NOT EXISTS): the
-- field's NAME says error — the load-bearing half for an operator is the READABLE
-- REASON ("partial: 2 chunks rejected"), which tells whether a partial embed is
-- content the provider will never accept; a timestamp duplicates updated_at. Two
-- writers disagreed (embeddings.py wrote reason strings, tasks.py time::now()) and
-- the string writes coerce-FAILED against the datetime type — flipping partial
-- embeds to 'failed' via the terminal branch. Precedent: schema.surql:334-336
-- (checkpoints content). The null-out below MUST precede the redefine: a row
-- carrying a datetime will not coerce to string (same failure mode the `archived`
-- backfill comment records); the migration migrate_last_embed_error_string.py
-- performs the same pair for runner-tracked DBs.
-- INVARIANT: the null-out clears ONLY a legacy datetime value, never a recorded
-- reason string. Why: schema.surql is re-applied on EVERY backend boot
-- (db_schema.apply_schema), so an unguarded null-out erased last_embed_error at
-- each restart — 35 dead-lettered prod docs all read NONE and the operator was
-- blind to WHY they failed for two months. The type guard keeps the pre-redefine
-- coercion fix (a datetime will not coerce to string) without destroying evidence.
UPDATE documents SET last_embed_error = NONE
  WHERE last_embed_error IS NOT NONE AND !type::is_string(last_embed_error);
DEFINE FIELD OVERWRITE last_embed_error ON documents TYPE option<string>;

-- documents: derived outline for the structure map's enumeration calls
-- (plan document-outline-in-structure-map). Raw headings, ALREADY rendered and
-- already degraded — the walk reads two plain columns instead of pulling
-- content per visible row. Scalars, not a nested object, partly for the
-- SCHEMAFULL trap (nested fields need explicit DEFINE FIELD to exist at all).
-- Written by doc_outline.refresh_outline from the embed job ABOVE _reembed, so
-- a project without embeddings still gets outlines; backfilled by the
-- runner-tracked document_outline_backfill migration. option-typed with no
-- non-option DEFAULT (the silent-write trap noted for sort_key above): a row
-- predating them reads NONE = "no outline".
DEFINE FIELD IF NOT EXISTS outline        ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS outline_hidden ON documents TYPE option<int>;

-- ─── 2026-05 Refactor: References unified onto documents ────────────────────
-- ARCH: References are documents with is_reference=true. The legacy tables
-- (notes, refs, parent_doc, parent_ref, mentions, ref_embeddings) and the
-- chat_sessions.reference_id / context_reference_ids COLUMNS are declared nowhere
-- in this file, and nothing reads or writes them: a reference-scoped chat stores
-- the reference id in chat_sessions.document_id (routes/chat/sessions.py).
-- Do not be misled by the same two names in routes/chat/serializers.py — those are
-- RESPONSE fields computed per request from context_ids, unrelated to the columns.
-- The columns and tables are NOT necessarily gone from a given database: dropping
-- them is a separate irreversible cutover gated behind MIGRATION_DROP_LEGACY=1 in
-- backend/migrations/migrate_2026_05_refactor.py, and it has not been run on every
-- environment — prod still carries them. Check INFO FOR DB per environment before
-- assuming; do not restate "were dropped" here.
-- INVARIANT: documents.parent_id of any document points to a non-reference
-- document (or NONE). Enforced by route handlers AND by the
-- documents_parent_check event below (defense in depth — the event THROW does
-- abort the CREATE/UPDATE in SurrealDB v3.0.2, verified empirically).

-- documents: reference flag + media fields
DEFINE FIELD IF NOT EXISTS is_reference      ON documents TYPE bool DEFAULT false;
-- WHY: OVERWRITE rather than IF NOT EXISTS — the ASSERT list gained 'file'
-- (agent-shared .zip archives land as media_type 'file' via the import_file
-- sandbox_path bridge). OVERWRITE re-applies the widened vocabulary on every boot;
-- IF NOT EXISTS would leave older DBs on the 3-value list and 500 every file-ref
-- create. Same precedent as processing_status below.
DEFINE FIELD OVERWRITE media_type        ON documents TYPE option<string>
    ASSERT $value = NONE OR $value IN ['markdown', 'audio', 'image', 'file'];
DEFINE FIELD IF NOT EXISTS source_url        ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS file_path         ON documents TYPE option<string>;
-- WHY: FLEXIBLE — file_meta sub-fields vary by media_type (image has width/
-- height, audio has duration, markdown has none). Declaring each sub-field
-- statically is fragile; FLEXIBLE lets the schema accept arbitrary children
-- without losing SCHEMAFULL strictness on other columns.
-- INVARIANT: FLEXIBLE must survive any future edit to this line. Why: without it a
-- SCHEMAFULL `documents` rejects every file_meta child it does not declare — an upload
-- fails with "Found field 'file_meta.duration'" (verified on 3.1.5, 2026-07-28).
DEFINE FIELD OVERWRITE file_meta             ON documents TYPE option<object> FLEXIBLE;
-- WHY: OVERWRITE rather than IF NOT EXISTS — Stage 1 defined the ASSERT list
-- without 'error', which is the canonical value used by the transcription
-- pipeline. Refs had no ASSERT; documents now must accept the same vocabulary.
DEFINE FIELD OVERWRITE processing_status ON documents TYPE option<string>
    ASSERT $value = NONE OR $value IN ['queued', 'processing', 'ready', 'error', 'failed', 'needs_reindex'];
-- Upload idempotency (SYSTEM: recording-cache): the recording cache's session
-- id, sent as idempotency_key by the upload routes — a replay of the same key
-- is served the EXISTING reference instead of a duplicate row. option: keyless
-- legacy uploads carry NONE. Verified on gray 2026-09-07 (surreal 3.x): two
-- keyless creates under the UNIQUE index both succeed (NONEs do not collide);
-- a second same-key create fails with "Database index … already contains '<key>'".
DEFINE FIELD IF NOT EXISTS idempotency_key ON documents TYPE option<string>;
DEFINE INDEX IF NOT EXISTS idx_documents_idem
    ON documents FIELDS idempotency_key UNIQUE;

DEFINE INDEX IF NOT EXISTS idx_documents_is_reference
    ON documents FIELDS project_id, is_reference, deleted_at;
DEFINE INDEX IF NOT EXISTS idx_documents_parent
    ON documents FIELDS project_id, parent_id, is_reference, deleted_at;
-- INVARIANT: parent_id-leading index for lookups that filter by parent_id WITHOUT a
-- project_id (right-panel reference list `parent_id IN $ancestors`, cascade-delete's
-- child scan, extractor's fetch_child_rows). Why: idx_documents_parent leads with
-- project_id, so a parent_id-only predicate can't use it and the planner falls back to
-- a full-table TableScan (~84ms on prod at 3098 docs). This index turns each parent_id
-- into an index point-seek (UnionIndexScan for the IN form). See references.py::list_references.
DEFINE INDEX IF NOT EXISTS idx_documents_parent_only
    ON documents FIELDS parent_id, is_reference, deleted_at;

-- Parent-check event: a document's parent must not itself be a reference.
-- SurrealDB v3 cannot express cross-record conditions in ASSERT, so we use a
-- table-level event that THROWs on violation. Verified: THROW inside the event
-- aborts the CREATE/UPDATE — the record is not persisted, and the driver
-- surfaces the error as a string return that create_record() raises.
DEFINE EVENT IF NOT EXISTS documents_parent_check ON documents
    WHEN $event = 'CREATE' OR $event = 'UPDATE'
    THEN {
        IF $after.parent_id != NONE {
            LET $parent = (SELECT is_reference FROM type::record('documents', $after.parent_id))[0];
            IF $parent.is_reference = true {
                THROW 'Reference documents cannot be parents';
            };
        };
    };

-- Reference-host invariant event: a reference (is_reference = true) MUST carry a real
-- host id in parent_id. The three no-host forms — NONE (what Python None serializes to
-- via CREATE ... CONTENT), NULL (raw inserts), and '' (legacy sentinel) — are all
-- rejected. Verified: THROW inside the event aborts the CREATE/UPDATE exactly like
-- documents_parent_check above.
-- INVARIANT (reference-host): is_reference = true ⇒ parent_id is a real, non-empty host
-- document id. Why: a no-host reference is invisible in EVERY document's reference panel
-- (the listing's `parent_id IN $ids` matches none of NONE/NULL/'', and get_ancestor_ids
-- never yields them) — a silent-data-loss defect, not a normal state. There is no single
-- code-layer chokepoint (REST, agent, Tool-API, move, migrations all write documents
-- directly), so the guarantee lives here at the one universal funnel — the table itself.
-- "Project level" is the ONE legitimate no-explicit-host intent and has exactly one
-- representation: parent_id = the project's index_doc_id (normalized at the writers that
-- mean it — references.py POST/PATCH, files_util.save_upload, files.py upload-markdown).
-- All other writers that produce a no-host reference are bugs and correctly abort here.
-- Cost: fires on every CREATE/UPDATE of every documents row, but short-circuits on the
-- common case (is_reference = false); the reference-row branch is three self-field
-- comparisons with no cross-record SELECT — cheaper than documents_parent_check above.
DEFINE EVENT IF NOT EXISTS documents_reference_parent_check ON documents
    WHEN $event = 'CREATE' OR $event = 'UPDATE'
    THEN {
        IF $after.is_reference = true AND ($after.parent_id IS NONE OR $after.parent_id IS NULL OR $after.parent_id = '') {
            THROW 'A reference must have a host document (parent_id)';
        };
    };

-- chat_sessions: note marker + anchor offsets in Unicode code points.
-- INVARIANT: anchor_offset_* are stored in code points (Python-native unit) and
-- converted to UTF-16 (CM6-native unit) only at the API serialization boundary.
DEFINE FIELD IF NOT EXISTS is_note             ON chat_sessions TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS anchor_offset_start ON chat_sessions TYPE option<int>;
DEFINE FIELD IF NOT EXISTS anchor_offset_end   ON chat_sessions TYPE option<int>;

DEFINE INDEX IF NOT EXISTS idx_chatsessions_is_note
    ON chat_sessions FIELDS project_id, is_note;

-- messages: per-message author (nullable; NONE ⇒ resolves to session.user_id).
-- INVARIANT: every code path that needs message authorship calls
-- resolve_message_author(message, session) — see backend/models.py.
DEFINE FIELD IF NOT EXISTS author_id ON messages TYPE option<string>;
DEFINE INDEX IF NOT EXISTS idx_messages_author ON messages FIELDS author_id;

-- INVARIANT: every `DEFINE FIELD ... TYPE <non-option> DEFAULT ...` added to
-- an existing table MUST get a matching backfill row here. DEFAULT applies only
-- on CREATE — pre-existing rows keep NONE and SurrealDB then rejects every
-- subsequent UPDATE on them with a coerce error, which `flush_to_db` swallows
-- via `except Exception`. The symptom is silent: edits ack on WS but never
-- reach DB. See incident 2026-05-28 (embedding_status, 394 docs affected).
-- Why: the failure is invisible until users notice their edits revert on
-- doc switch; backend logs only show `logger.exception` inside the periodic
-- flush loop, which is easy to miss. Backfilling at schema-apply time is the
-- cheapest way to keep the trap from re-arming.
UPDATE documents     SET is_reference     = false WHERE is_reference     = NONE;
UPDATE documents     SET embedding_status = 'ok' WHERE embedding_status = NONE;
UPDATE chat_sessions SET is_note          = false WHERE is_note          = NONE;

-- ─── Chat session agent fields ──────────────────────────────────────────────
-- ARCH (plan: remove-ask-line-mode-axis): the `mode` column is VESTIGIAL — it
-- stays in the schema (dropping a column on 660+ live rows buys nothing and is
-- irreversible) but NO code writes or reads it. Stage 9 deleted the Ask/read-only
-- line; every AI chat is an agent chat, so the column selects nothing. New rows
-- take the DEFAULT 'chat'; historical rows keep their old value; neither is read.
-- The surviving selector is `agent_auto` (confirm <-> auto-apply).
--
DEFINE FIELD IF NOT EXISTS mode          ON chat_sessions TYPE string DEFAULT 'chat'
    ASSERT $value IN ['chat', 'agent'];
DEFINE FIELD IF NOT EXISTS target_doc_id ON chat_sessions TYPE option<string>;
-- ARCH (plan remove-chat-branch-lock): the apply-lock fields are deleted —
-- a chat is never frozen; editing a message and forking from it stay available
-- for the life of a session, including after a tool has applied a document
-- mutation. Off-path siblings were only ever HIDDEN (kept in DB), so removing
-- the filter reveals data that is already intact. REMOVE keeps the edit
-- idempotent on fresh AND stale DBs (mirrors the removals below); the runner's
-- chat_apply_lock_fields_drop migration owns the same REMOVE for existing DBs.
REMOVE FIELD IF EXISTS has_applied_edit ON chat_sessions;
REMOVE FIELD IF EXISTS frozen_anchor_message_id ON chat_sessions;
-- ARCH (Issue 3 — agent_auto persistence): the agent-role dropdown's "Full auto"
-- state is persisted as a backend column (mirrors model/system_prompt_id), NOT an
-- in-memory UI map — so it survives reload. The UI mode is DERIVED:
-- mode==='agent' ? (agent_auto ? 'agent_auto' : 'agent_confirm') : 'readonly'.
-- INVARIANT: switching away from agent forces agent_auto=false so re-entering
-- agent mode always starts in confirm mode (no stale auto-apply grant).
DEFINE FIELD OVERWRITE agent_auto ON chat_sessions TYPE option<bool> DEFAULT false;
-- ARCH (plan: selection-region-agent): a pinned-region agent chat forces confirm
-- mode + a per-apply containment check constraining the agent to a frontend-tracked
-- text span. Server-authoritative (survives cross-device reload); the RelativePosition
-- pair is frontend-owned (localStorage). option<bool> + OVERWRITE avoids the NONE-
-- coerce-on-UPDATE trap (mirror of agent_auto) for pre-existing rows.
DEFINE FIELD OVERWRITE has_region ON chat_sessions TYPE option<bool> DEFAULT false;
-- ARCH: the driver owns every turn — there is no transport to pick, so no
-- `lifecycle` column. This REMOVE is the fresh-DB twin of the
-- chat_lifecycle_drop migration (which covers DBs created while the column
-- lived).
REMOVE FIELD IF EXISTS lifecycle ON chat_sessions;
-- ARCH (plan: session-title-from-the-harness): the session title is minted by
-- the harness titler and relayed as session/title; the backend's relay write
-- lands ONLY while this flag is falsy. A PATCH title is a USER rename and sets
-- it — dsh never learns of our renames (no ctx.sessionTitle.rename() on the
-- PATCH path), so without the pin the next automatic revision would overwrite
-- the user's title. The guard lives at the WRITE (turn_persistence.
-- _persist_session_title), not in the plugin. option<bool> + OVERWRITE avoids
-- the NONE-coerce-on-UPDATE trap (mirror of agent_auto).
DEFINE FIELD OVERWRITE title_user_set ON chat_sessions TYPE option<bool> DEFAULT false;
-- ARCH: last-known context occupation for the token-usage gauge.
-- `context_tokens_used` = tokens the active session's discussion occupies. Updated
-- once per NORMAL turn from the driver's context_usage signal (NOT on
-- error/disconnect — an abnormal turn has no honest final state). option<int> so
-- pre-existing rows back-fill to NONE; the serializer emits null and the client
-- resolves the live cap from /models (context_windows).
DEFINE FIELD IF NOT EXISTS context_tokens_used ON chat_sessions TYPE option<int>;
-- context_tokens_cap was dropped (plan 1785795000000): the persisted cap was a
-- source that could never disagree with the fallback it preceded. REMOVE is
-- idempotent on fresh AND stale DBs (mirrors token_hash/seq removals).
REMOVE FIELD IF EXISTS context_tokens_cap ON chat_sessions;
-- ARCH (plan collapse-agent-stack-onto-dsh-vocabulary step 5): the freeze
-- subsystem (archived / archived_from / archived_at_leaf + the banner + the
-- legacy-line migrations) is deleted — the release that ships this wipes all
-- chat sessions, so there is nothing left to mark frozen. REMOVE keeps the
-- edit idempotent on fresh AND stale DBs.
REMOVE FIELD IF EXISTS driver_line ON chat_sessions;
REMOVE FIELD IF EXISTS archived ON chat_sessions;
REMOVE FIELD IF EXISTS archived_from ON chat_sessions;
REMOVE FIELD IF EXISTS archived_at_leaf ON chat_sessions;
-- The continuation chat_sessions row minted after compaction:
-- `compacted_from` records the source chat_session id (idempotency +
-- traceability).
DEFINE FIELD IF NOT EXISTS compacted_from ON chat_sessions TYPE option<string>;
-- The compaction WINDOW the row was minted for: the source session's leaf
-- after compaction (the CompactionEntry).
-- INVARIANT: the continuation idempotency guard keys on
-- (source, leaf), never on the source id alone. Why: a long chat compacts more
-- than once and the mint is retried on failure — id alone cannot tell
-- the two apart, and swallowing a second compaction as a retry minted a second
-- continuation (duplicated history).
DEFINE FIELD IF NOT EXISTS compacted_at_leaf ON chat_sessions TYPE option<string>;

-- Backfill pre-existing chat sessions (mode defaults to 'chat').
UPDATE chat_sessions SET mode = 'chat' WHERE mode = NONE;
UPDATE chat_sessions SET agent_auto = false WHERE agent_auto IS NONE;
UPDATE chat_sessions SET title_user_set = false WHERE title_user_set IS NONE;

-- ARCH: the embedded `proposal` (singular) and `proposals` (plural array)
-- fields + their nested sub-field definitions USED to live here. They are
-- GONE by design, and the `tool_proposals` rows table is DROPPED with them
-- (plan retire-the-proposal-card-render-cluster): the proposal cluster's
-- writer went first, then the render tail — current turns persist through
-- turn_persistence, and old threads render through the agent_steps chips.
-- WHY NOT HERE: apply_schema re-runs every boot (main.py:124) BEFORE the migration
-- runner, and its `DEFINE FIELD IF NOT EXISTS`/`OVERWRITE` would RE-CREATE
-- whatever the one-time `_migrate_proposals_to_rows` REMOVE FIELD dropped —
-- leaving dead field definitions resurrected on every boot forever. A field
-- removed by a runner migration must NOT also be (re)declared in schema.surql.
-- The `_migrate_proposals_to_rows` entry does copy-then-REMOVE; nothing
-- destructive belongs in this idempotent DDL file.
DEFINE FIELD IF NOT EXISTS agent_trace ON messages TYPE option<object> FLEXIBLE;

-- `segments` / `agent_steps` / `reasoning` (the stored turn timeline) are
-- DELETED (plan retire-the-stored-turn-timeline step 3): the driver's log
-- replayed as `frames` is the timeline, and `halt` below keeps the one fact
-- the log cannot produce. The runner migration messages_timeline_columns_drop
-- sweeps the data and REMOVEs the fields — do NOT redeclare them here (same
-- rule as the proposals WHY-NOT-HERE above).

-- ARCH (plan retire-the-stored-turn-timeline step 1): the abnormal-end halt
-- card moved OUT of the `segments` array into its own column. `segments` was
-- the second transcript; its one irreplaceable member — the halt card an
-- abnormally ended turn must show after a reload — is what THIS column keeps.
-- Written ONLY by _persist_projection_extras (driver/persistence.py) on an
-- abnormal end; a graceful `turn_halted` persists nothing Lore-side (the
-- driver's own log is that turn's timeline). FLEXIBLE so the one-time backfill
-- can copy the legacy halt SEGMENT verbatim (`type` key included) — new writes
-- carry only reason/steps/limit.
DEFINE FIELD IF NOT EXISTS halt        ON messages TYPE option<object> FLEXIBLE;
-- OVERWRITE + option<> (not IF NOT EXISTS + string): the timeline-column sweep
-- (messages_timeline_columns_drop) re-coerces every row, and on a table of
-- SCHEMALESS kind a declared REQUIRED subfield fails that coercion on every
-- row whose `halt` parent is absent (`Expected string but found NONE`) — a
-- card may carry only steps/limit. OVERWRITE so an existing DB's
-- required-string definition is retyped BEFORE the migration runs (apply_schema
-- precedes the runner); option<> is the data truth: nothing writes a
-- reason-less card today, the option only tolerates one.
DEFINE FIELD OVERWRITE halt.reason ON messages TYPE option<string>;
DEFINE FIELD IF NOT EXISTS halt.steps  ON messages TYPE option<int>;
DEFINE FIELD IF NOT EXISTS halt.limit  ON messages TYPE option<int>;

-- ARCH: the DETACHED generate_image run's chips (the refiner chip and the image
-- chip) ride their own column, for the same reason `halt` above does: the driver's
-- log cannot produce them. The generation finishes in an arq task long after the
-- tool call returned {status:"generating"}, so the log holds the call and nothing
-- else, and the refined SD prompt exists ONLY here and in the done event (it is
-- never returned to the agent). Written by the background task
-- (image_gen/persist.py) before the generate_image_done emit; the client appends
-- them to the replayed turn product.
DEFINE FIELD IF NOT EXISTS gen_steps ON messages TYPE option<array<object>> FLEXIBLE;
-- INVARIANT(data-loss): the array's ELEMENTS carry their own FLEXIBLE, not just the
-- parent above. Why: on a SCHEMAFULL `messages` (prod, and every test DB) SurrealDB
-- materializes `array<object>` into `gen_steps.* TYPE object`, and the parent's FLEXIBLE
-- does not reach it — every child key of a step is then rejected, the whole append
-- throws, and the refiner chip and the image chip are absent from every reload with the
-- generation itself having succeeded. Bound by test_schema_drift_roundtrip.py.
-- OVERWRITE, not IF NOT EXISTS: the unwanted definition already exists on prod (same
-- reason halt.reason above is OVERWRITE), and IF NOT EXISTS would leave it standing.
DEFINE FIELD OVERWRITE gen_steps.* ON messages TYPE object FLEXIBLE;

-- messages: the dsh session-log seq of the row's turn/end — with
-- driver_session below, the driver id the Lore projection keeps. The fork seam
-- (seam A) resolves the branch point from the pair. NONE only when no
-- terminal frame carried a seq (a deadline breach or disconnect with no
-- turn/end, a pre-harness thread) — an abnormally ended turn whose terminal
-- frame DID carry one is stamped like any other: a fork at an unstamped row
-- walks to the nearest stamped ancestor (an abnormal row) or refuses
-- honestly (an unaccounted thread), never a silent root reset.
DEFINE FIELD IF NOT EXISTS driver_seq ON messages TYPE option<int>;
-- The dsh session whose log `driver_seq` belongs to — the other half of the
-- branch point. dsh seqs are per-session and a root fork restarts them at 0,
-- so a seq alone cannot name a boundary on an abandoned branch. NONE on rows
-- stamped before the pair existed (the leaf then resolves against the
-- current session) and when the subscribe ack carried no dsh id.
DEFINE FIELD IF NOT EXISTS driver_session ON messages TYPE option<string>;

-- ─── CRDT: Y.Doc persistence (append-only update log + compacted snapshot) ──
-- ARCH: content is now a derived read-model. The authoritative document state is the
-- Y.Doc snapshot in ydoc_state, replayed with newer ydoc_updates on load. Compaction
-- merges the log into ydoc_state + derived content, pruning the log, under a per-doc lease.

DEFINE TABLE IF NOT EXISTS ydoc_updates SCHEMAFULL;
REMOVE FIELD IF EXISTS seq ON ydoc_updates;
REMOVE INDEX IF EXISTS idx_ydoc_updates_doc_seq ON ydoc_updates;
DEFINE FIELD IF NOT EXISTS document_id ON ydoc_updates TYPE string;
DEFINE FIELD IF NOT EXISTS payload     ON ydoc_updates TYPE bytes;
DEFINE FIELD IF NOT EXISTS created_at  ON ydoc_updates TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_ydoc_updates_doc_created ON ydoc_updates FIELDS document_id, created_at;

-- documents: CRDT compacted snapshot (bytes). NONE = no CRDT state yet (pre-migration).
DEFINE FIELD IF NOT EXISTS ydoc_state ON documents TYPE option<bytes>;

-- documents: persist degraded save state so reconnecting readers are warned.
-- INVARIANT: set to time::now() when consecutive flush failures exceed threshold;
-- cleared to NONE on next successful flush. Frontend checks on collab connect.
DEFINE FIELD IF NOT EXISTS last_save_failed_at ON documents TYPE option<datetime>;

-- chat_sessions: CRDT relative positions for note anchors (binary StickyIndex).
-- INVARIANT: anchor_rel_start/end replace anchor_offset_start/end after cutover.
-- Both int and bytes columns coexist during migration; int columns are dropped later.
DEFINE FIELD IF NOT EXISTS anchor_rel_start ON chat_sessions TYPE option<bytes>;
DEFINE FIELD IF NOT EXISTS anchor_rel_end   ON chat_sessions TYPE option<bytes>;

-- ─── Pi sessions tables (pi_sessions / pi_session_entries) — RETIRED ────────
-- No reader, no writer, no pending migration; nothing here defines them anymore.

-- ─── Instance settings (SYSTEM: instance-settings) ──────────────────────────
-- One row per OVERRIDDEN key: row exists = override active; DELETE = reset to
-- env/default (never a null write). value is a JSON-encoded string so a single
-- option<string> column carries the per-key type (int/float/bool/str/secret);
-- settings_registry owns the decode. updated_by/updated_at serve the UI chip —
-- the audit trail is the app log, not a history table.
DEFINE TABLE IF NOT EXISTS instance_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS key        ON instance_settings TYPE string;
DEFINE FIELD IF NOT EXISTS value      ON instance_settings TYPE option<string>;
DEFINE FIELD IF NOT EXISTS updated_by ON instance_settings TYPE string;
DEFINE FIELD IF NOT EXISTS updated_at ON instance_settings TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_instance_settings_key ON instance_settings FIELDS key UNIQUE;

-- ─── Instance skills (overlay middle layer: project > instance > shipped) ────
-- name is the overlay identity: an instance row of a shipped skill's name
-- SHADOWS the shipped file; enabled=false is a tombstone that suppresses the
-- shipped skill of that name. content is raw markdown with the same frontmatter
-- contract the harness plugin parses (null on a pure tombstone row).
DEFINE TABLE IF NOT EXISTS instance_skills SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name       ON instance_skills TYPE string;
DEFINE FIELD IF NOT EXISTS content    ON instance_skills TYPE option<string>;
DEFINE FIELD IF NOT EXISTS enabled    ON instance_skills TYPE bool DEFAULT true;
DEFINE FIELD IF NOT EXISTS updated_by ON instance_skills TYPE string;
DEFINE FIELD IF NOT EXISTS updated_at ON instance_skills TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_instance_skills_name ON instance_skills FIELDS name UNIQUE;
UPDATE instance_skills SET enabled = true WHERE enabled IS NONE;
