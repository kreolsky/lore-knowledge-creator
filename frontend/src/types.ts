/**
 * Shared TypeScript interfaces mirroring backend API response shapes.
 *
 * These interfaces are hand-authored and are the ONLY API types the frontend
 * consumes. The tracked repo-root openapi.json remains the backend surface
 * contract: CI (backend-lint) regenerates it from the live route registry and
 * fails on drift, so a backend response_model / route-surface change that goes
 * un-regenerated breaks CI. Keeping these shapes in sync with that contract is
 * a manual step reviewed at the seam, not a codegen.
 */

export type AccessLevel = 'full' | 'commentator' | 'readonly';

export type Language = 'en' | 'ru';

/** Instance role — mirrors the backend Literal (models/auth.py). */
export type Role = 'admin' | 'moderator' | 'user';

export interface User {
  user_id: string;
  name: string;
  email: string;
  role: Role;
  has_pin: boolean;
  /** Provenance: uid of the account creator (creating manager / invite's invited_by). */
  created_by?: string | null;
  /** Group head (a moderator's uid) — legal only on role='user' rows; null otherwise. */
  moderator_id?: string | null;
  /** Display names for created_by / moderator_id, resolved by GET /api/admin/users
   * (a moderator's scoped list never contains the referenced rows). */
  created_by_name?: string | null;
  moderator_name?: string | null;
  /** Capability flags from /api/auth/me (+ login) — the UI gates on these,
   * never on role string comparisons in TSX. */
  is_admin?: boolean;
  can_manage_users?: boolean;
  /** Free-text biographical notes about the user (editable by user themselves). */
  user_facts: string;
  /** IANA timezone (e.g. "Europe/Moscow"), auto-detected by the client on session start. */
  timezone?: string | null;
}

/** A user currently connected to the active collab entity (presence chips). */
export interface PresenceUser {
  user_id: string;
  name: string;
  /** Server-side access level for this entity. Lets the UI distinguish editors
   * (full) from viewers/commentators — e.g. the Info-tab "editing now" header only
   * counts editors, mirroring the server's _has_pushed discriminator. */
  access_level: AccessLevel;
}

export interface Project {
  project_id: string;
  name: string;
  /** User-facing short description shown on the dashboard card. Null when unset. */
  description?: string | null;
  status: 'active' | 'paused' | 'done';
  /** Markdown text with project-wide instructions and lore context. Stored in the index document. */
  project_context: string;
  index_doc_id: string | null;
  voice_recording_doc_id: string | null;
  /** Project-wide: the references panel shows image refs as a gallery (true) or as cards. */
  ref_image_preview?: boolean;
  last_accessed_doc_id: string | null;
  owner_id: string | null;
  owner_name?: string | null;
  is_public: boolean;
  /** Caller's access level for this project, computed server-side from project_members. Null if no access. */
  my_access: AccessLevel | null;
  /** Whether the caller is a project root (the owner, or an admin with a member
   * row) — backend-computed capability flag (backend: is_project_root). */
  is_owner_like?: boolean;
  created_at: string;
  /** Non-owner project member count (excluding owner). 0 when the project is solo. */
  members_count?: number;
}

export interface ProjectMember {
  /** Empty string when this row represents a pending invite (no user yet). */
  user_id: string;
  email: string;
  name: string;
  access_level: AccessLevel;
  pending?: boolean;
}

export interface SearchSnippet {
  before: string;
  match: string;
  after: string;
}

export interface SearchResult {
  document_id?: string;
  reference_id?: string;
  title: string;
  match_count: number;
  snippet: SearchSnippet | null;
  media_type?: string;
  created_at: string;
  updated_at: string | null;
}

export interface HeadingItem {
  level: number;
  text: string;
  line: number;
}

export interface Document {
  document_id: string;
  project_id: string;
  parent_id: string | null;
  title: string;
  content: string;
  path: string;
  is_index: boolean;
  // Fractional sort key for sibling order within the tree (newest-first base order).
  // Optional: partial docs from WS create events may arrive before the key is known.
  sort_key?: string;
  headings?: HeadingItem[];
  created_at: string;
  updated_at: string;
  last_save_failed_at?: string | null;
  // Reserved agent-brain subtree markers. Present on
  // system docs returned by the tree payload; undefined for ordinary documents.
  // The persona picker selects direct children of the Personas folder (the doc
  // with system_role === 'system_prompt') by parent_id — not by role tag.
  is_system?: boolean;
  system_role?: string | null;
  // Caller's own non-internal key capabilities on this doc (server-derived,
  // plan "glimmering-knitting-pebble"). Omitted when no key exists. Drives the
  // doc-tree key indicator and ParentPickerPopup's reparent warning.
  key_capabilities?: string[];
  // Owner-only flag: this doc is covered by a live anonymous public link
  // (document_shares). Drives the orange doc-tree icon (plan
  // "orange-tree-icon"); absent for non-owners and unshared docs. Precedence
  // vs key_capabilities is resolved in utils/key-icon.ts (agent > public > widget).
  public_share?: boolean;
  // PUBLIC SHARE ONLY: live Yjs tables subtree (JSON) captured server-side by
  // /api/public/{token}/documents/{id}. The public Editor seeds its table widgets
  // from this (mirrors snapshot-preview seeding). Undefined on the authed surface
  // (the live editor reads tables from the collab Yjs handle).
  tables_json?: string | null;
}

export interface DocumentTreeNode extends Document {
  children: DocumentTreeNode[];
}

export interface Checkpoint {
  checkpoint_id: string;
  document_id: string;
  content?: string;
  // Lossless capture of the editable-table state paired with `content`'s anchors
  // (`![label](table:id)`). Returned by GET /api/checkpoints/{id} (full row serialize);
  // null for legacy rows that predate table capture. Drives the snapshot-preview's
  // read-only table rendering (see Editor.tsx seedPreviewTablesDoc).
  tables_json?: string | null;
  label: string;
  comment: string;
  created_by?: string | null;
  user_name?: string;
  created_at: string;
}

export interface DocumentHistoryEntry {
  id: string;
  document_id: string;
  user_id: string | null;
  user_name: string;
  action: 'created' | 'edited';
  created_at: string;
}

export interface FileMeta {
  mime_type?: string;
  file_size?: number;
  duration_sec?: number;
  width?: number;
  height?: number;
  original_name?: string;
}

export interface Reference {
  reference_id: string;
  project_id: string;
  document_id: string | null;
  title: string;
  /** 'file' = a downloadable binary shared by the agent (zip archives today). */
  media_type: 'markdown' | 'audio' | 'image' | 'file';
  source_url: string | null;
  /**
   * The reference body. Optional since the LIST endpoint is metadata-only
   * (content is lazy-fetched per reference via GET /references/{id}). Absent
   * (undefined) on list-sourced refs; present on single-GET / POST / PATCH /
   * upload / status responses and after client-side hydration.
   */
  content?: string;
  /**
   * True when the reference has a non-empty body server-side. Carried ONLY by
   * the LIST endpoint (metadata-only serializer); absent on single-GET/POST/etc.
   * Lets the UI distinguish "empty ref" (false) from "content not yet loaded"
   * (true + `content === undefined`) so a content-less ref shows a loading
   * state instead of an empty editor (no-silent-degradation).
   */
  has_content?: boolean;
  /**
   * see SYSTEM: inbox — VIEWER-relative unread flag: this reference arrived
   * from OUTSIDE (a widget key or an MCP key) for the CURRENT user and has not
   * been opened. Always present on the authed LIST (false for everyone else —
   * the raw recipient id never crosses the wire); absent on public-share rows.
   */
  unread?: boolean;
  /** Audio/image processing pipeline status. Null for markdown refs (no processing needed). */
  processing_status: 'uploading' | 'queued' | 'processing' | 'ready' | 'error' | null;
  file_path: string | null;
  file_meta: FileMeta | null;
  /** Soft-archived: hidden from the default LIST, restorable — archived refs dim
   * in place, sink below live refs when
   * "Show archived" is on, and a second trash click soft-deletes them. */
  archived?: boolean;
  /**
   * Persisted manual order within the reference's host group: `(sort_key, reference_id)` ASC inside a tier; a
   * new ref mints the TOP key of its group. Absent only on rows the
   * reference_sort_keys_backfill migration has not reached (reads as '').
   */
  sort_key?: string | null;
  headings?: HeadingItem[];
  updated_at: string;
  created_at: string;
  /**
   * The reference CREATOR's identity + denormalized nickname (plan
   * reference-card-author-nickname). Surfaced so RefCard can show "<nick>" as the
   * last meta segment for someone ELSE's reference. Absent (undefined/null) for
   * impersonal/legacy creations → the UI renders no author segment.
   */
  created_by?: string | null;
  created_by_name?: string | null;
}

/** ONE key row with a capabilities set:
 * widget = audio widget on the bound doc, agent = MCP/Tool-API sandboxed to the
 * doc's subtree. The plaintext token is shown only once at mint time. */
export interface ApiKey {
  key_id: string;
  label: string;
  document_id: string;
  capabilities: string[];
  auto_apply?: boolean;
  created_at: string;
  last_used_at: string | null;
}

export interface AgentConfig {
  config_id: string;
  document_id: string;
  config_doc_id: string;
  target_doc_id: string;
  project_id: string;
  trigger_event: string;
  title_template: string | null;
  model: string | null;
  created_at: string;
}

// ─── AI Chat ─────────────────────────────────────────────────────────────────

// ARCH: the agent line (harness) is the ONLY AI line — every AI chat is an agent
// chat. There is no
// Ask/read-only line and no `mode` axis on the wire, and so no `ChatMode`
// (ask|agent) Literal. The only selector is `agent_auto` (confirm <->
// auto-apply), expressed as `ChatUIMode`.

// ARCH: a live-resolved pinned text region. The
// frontend owns the Yjs RelativePosition pair (localStorage) and resolves it to
// (from_cp, to_cp, text) per turn/apply. Wire shape matches the backend RegionRef.
// Offsets are Unicode CODE POINTS — convert from CM6's UTF-16 units at the bridge.
export interface RegionRef {
  doc_id: string;
  from_cp: number;
  to_cp: number;
  text?: string | null;
}

// The frontend-owned pinned-region anchor: a doc_id + a Yjs RelativePosition JSON
// pair ( survives edits via the CRDT). Persisted in localStorage keyed by session.
export interface PinnedRegion {
  doc_id: string;
  relFrom: unknown; // Y.relativePositionToJSON(...) — opaque JSON
  relTo: unknown;
}

// WHY: the apply-mode selector. With the
// Ask line gone there is no 'readonly' — every AI chat is an agent chat; the only
// choice is confirm vs auto-apply. Kept as a 2-option union (the former 3-option
// name is preserved to avoid a separate rename refactor).
export type ChatUIMode = 'agent_confirm' | 'agent_auto';

// WHY (agent_auto persistence): the agent-role dropdown is DERIVED
// from the persisted `agent_auto` column, not an in-memory UI map. This is the
// single read-time helper used by the store send paths + ChatInput. The choice
// survives reload because it is a backend column (mirrors model/system_prompt_id).
//
// There is no `mode` read — every AI chat is an agent chat, so the result is
// driven solely by `agent_auto`.
export function deriveUIMode(
  session: { agent_auto?: boolean } | null | undefined,
): ChatUIMode {
  if (!session) return 'agent_confirm';
  return session.agent_auto ? 'agent_auto' : 'agent_confirm';
}

// WHY: canonical WRITE-time inverse of deriveUIMode. Maps the apply-mode
// selector back to the persisted agent_auto bool. Single source so every write
// path (setSessionUIMode ghost + session branches, ChatInput memo) stays in
// lockstep with deriveUIMode — adding a ChatUIMode value requires updating only
// these two helpers, not scattered call sites.
// There is no `mode` half (no wire field); only agentAuto is returned.
export function inverseUIMode(mode: ChatUIMode): { agentAuto: boolean } {
  return { agentAuto: mode === 'agent_auto' };
}

// One still-held mutating call (mid-turn approval): the backend is parking the
// Tool-API request until the user decides (allow once / allow for the session /
// reject). The `lore/verdict-ask` mint creates these live; GET /chat/verdicts
// restores them after reload. The resolved call's tool result removes the entry.
export interface PendingVerdict {
  call_id: string;
  tool_name: string;
  message_id: string;
}

// One executed/decided step in the agent loop (search/read auto-run; create/edit
// go through the Tool-API and leave a step too). Rendered as a chip in the
// step-trace for transparency.
export interface AgentStep {
  tool_call_id?: string;
  /** The DISPATCHING call this step ran under (a subagent child's activity):
   * set on child-session steps (their tool_call_id is child-session-derived);
   * the timeline renders them nested inside the dispatching chip. */
  parent_call_id?: string;
  tool: string;
  /** The tool's OWN output text, stored verbatim — the chip body. Absent on
   *  rows written before the chip-presentation layer was deleted; those carry
   *  `summary`/`detail` instead and render through the fallback. */
  output?: string;
  /** LEGACY — a one-line paraphrase, written by the deleted presentation
   *  layer. Kept so pre-deletion threads still render; nothing writes it. */
  summary?: string;
  /** LEGACY — the paraphrased call data. Same reason as `summary`. */
  detail?: string;
  outcome?: 'failed' | 'noop';
  /**
   * The generated image's reference id, stamped on
   * a `generate_image` applied step. When set, the chat chip renders a thumbnail
   * (click → full-image lightbox). Absent on every other step (old rows + non-
   * image steps render unchanged). Rides the driver's replayed frames, so a
   * reload re-renders it.
   *
   * LEGACY singular form — kept for back-compat with old persisted rows. New rows
   * carry the plural `image_ref_ids`; the renderer derives the
   * id list via `stepImageIds(step)` so both shapes render.
   */
  image_ref_id?: string;
  /**
   * The generated images' reference ids (a batch of N), stamped
   * on a `generate_image` applied step with count > 1. When set, the chat chip
   * renders N thumbnails in one chip ("one pin"); any thumbnail opens ONE shared
   * lightbox with ←/→ navigation across the pin.
   * Absent on singular generations + non-image steps. Rides the driver's
   * replayed frames, so a reload re-renders all N thumbnails.
   */
  image_ref_ids?: string[];
  /**
   * The detached generation's run_id, stamped
   * on a `generate_image` step delivered asynchronously (the chip's tool_call_id is
   * `gen:{run_id}`). Informational correlation; the renderer does not require it.
   */
  run_id?: string;
  /**
   * Bounded, intent-bearing subset of the call args
   * (query / document_id / prompt / flags / counts …), declared at the tool
   * (the plugin's `presentationMeta`, relayed as `tool_result.meta`). Shown in the chip's "Вызов" block. Absent on
   * sandbox_bash (the command lives in `detail`) and on old persisted rows → the
   * renderer keeps the `detail ?? summary` fallback. Free-form/large values are
   * dropped at the declaration (no secrets, no bloat).
   */
  args?: Record<string, unknown>;
  /**
   * Bounded per-tool slice of the tool result (search
   * hits, web results, structure nodes, memory entities, read_document head, …),
   * declared at the tool (the plugin's `presentationMeta`, relayed as
   * `tool_result.meta`). Shown in the chip's
   * "Ответ" block. Absent on sandbox_bash + failed mutations (they keep `detail`)
   * and on old rows → the renderer keeps the `detail ?? summary` fallback. Shape
   * is per-tool (a tagged union keyed by `tool` would mirror the declaration, but the
   * renderer switches on `tool` anyway, so `unknown` keeps it forward-compatible
   * without re-declaring every slice here).
   */
  result?: unknown;
}

/**
 * The id list to render for a generate_image agent step.
 * Plural `image_ref_ids` wins; absent → derive a one-element list from the legacy
 * singular `image_ref_id`; both absent → empty (no thumbnail). Single source so
 * the pin-open checks + the thumbnail row agree, and so old persisted rows
 * (singular) + new rows (plural) render identically.
 */
export function stepImageIds(step: { image_ref_ids?: string[]; image_ref_id?: string }): string[] {
  if (step.image_ref_ids && step.image_ref_ids.length > 0) return step.image_ref_ids;
  return step.image_ref_id ? [step.image_ref_id] : [];
}

// The abnormal-end halt vocabulary, rendered as the halt card. `steps`/`limit`
// are present only on a HISTORICAL `step_limit` halt (the deleted turn_budget's
// label — persisted rows still carry the counters; no live halt reason emits
// them). `output_token_limit` is the live model-side max-tokens halt;
// `step_limit` stays in the union so old rows keep rendering. `disconnected` is
// minted by the backend's disconnect-finalization path (the frame consumer dropped
// mid-turn) — it carries `steps` but no `limit` and no Continue button (resume
// is re-running consolidate_memory, which auto-resumes).
export type HaltReason =
  | 'tool_call_limit'
  | 'repeat_limit'
  | 'step_limit'
  | 'output_token_limit'
  | 'context_limit'
  | 'disconnected';

// ARCH (plan reasoning-effort-selector): the gateway's per-model reasoning
// capability — a GET /chat/models `reasoning` map entry, projected backend-side
// from the gateway's /v1/capabilities. effort_levels are the model's ADVERTISED
// levels, rendered verbatim by the composer dropdown (the router owns the
// spelling; the harness resolves Default).
export interface ReasoningCapability {
  supported: boolean;
  effort_levels: string[];
}

export interface ChatSession {
  session_id: string;
  project_id: string;
  document_id: string | null;
  reference_id: string | null;
  // ARCH: the document-session's
  // own title, served by the backend (thin client). Present on list rows; null on
  // create/update responses (the client falls back to its document tree). A
  // ref-scoped chat carries reference_title instead (document_title is null there).
  document_title?: string | null;
  // WHY: a reference-scoped chat's parent IS the reference (ref: prefix),
  // never its owning document — even when created in split view. reference_title is
  // the reference's OWN title (backend serializer). Why: the chat's identity is the
  // entity it was created on (a recurring user rule).
  reference_title?: string | null;
  user_id: string;
  title: string;
  model: string;
  // ARCH (line pinning): the agent line this session is pinned to ('pi' |
  // 'harness'), written once at create. There is no UI picker (the pilot is
  // operator/API-only); the client renders it, the backend routes on it.
  system_prompt_id: string | null;
  // ARCH (plan reasoning-effort-selector): the per-session reasoning effort.
  // null = Default (no reasoning_effort on the wire, the provider's default
  // applies); an explicit
  // value must be in the target model's advertised list (the PATCH guard 400s
  // outside it). Reset to null on model change. Optional like
  // context_tokens_used — wire-authored, never client-constructed.
  reasoning_effort?: string | null;
  context_ids: string[];
  // ARCH: server-authored
  // ordered subset of context_ids that are references (is_reference=true). The
  // single source of truth for the doc/ref split — the client consumes it
  // directly so a cross-doc reference is never misclassified as a document by
  // the open doc's scope-limited reference set. Optional: older servers omit it
  // and the client falls back to its refIdSet split.
  context_reference_ids?: string[];
  is_note?: boolean;
  /**
   * see SYSTEM: inbox — VIEWER-relative unread flag: this NOTE arrived from
   * OUTSIDE (a widget key) for the CURRENT user and has not been opened. The
   * raw recipient id never crosses the wire; false for everyone else's view.
   */
  unread?: boolean;
  // ARCH: note display fields
  // derived live from messages by the backend (never read `title` for notes).
  // first/last preview + message_count stay note-only (is_note=true); undefined
  // for AI chats.
  first_message_preview?: string | null;
  last_message_preview?: string | null;
  message_count?: number;
  // ARCH: present on EVERY session (notes +
  // AI) — the time of the last message, derived read-time by the backend. Null
  // for a freshly-created / just-PATCHed row; the UI falls back to updated_at.
  last_message_at?: string | null;
  anchor_offset_start?: number | null;
  anchor_offset_end?: number | null;
  anchor_rel_start?: string | null;
  anchor_rel_end?: string | null;
  // ARCH: `mode` is no longer on the wire
  // (the Ask line + its axis are deleted). The backend serializer drops the
  // column; every AI chat is an agent chat. The surviving selector is agent_auto.
  target_doc_id?: string | null;
  // WHY (agent_auto persistence): persisted "Full auto" dropdown state.
  // The UI mode is DERIVED from agent_auto via deriveUIMode(); survives reload.
  agent_auto?: boolean;
  // ARCH: a pinned-region agent chat constrains the
  // agent to a frontend-tracked text span. Server-authoritative (forces confirm +
  // the containment gate); the RelativePosition pair is frontend-owned (localStorage).
  has_region?: boolean;
  // ARCH (plan session-title-from-the-harness): the title pin — a PATCH title
  // (user rename) sets it backend-side, and the harness titler's relayed
  // revision skips pinned rows. Frontend never writes it; declared because
  // `SELECT *` ships the column to the client.
  title_user_set?: boolean;
  // ARCH: last-known context occupation for the token-usage gauge — the tokens
  // the active session's discussion occupies. Updated once per NORMAL turn from
  // the context_usage frame; null before the first turn / pre-migration. The
  // persisted figure is used only — the cap below is live, never persisted.
  context_tokens_used?: number | null;
  // ARCH: the gauge's live denominator, taken from the LAST context_usage
  // frame's cap (the harness projection + Lore's threaded context_window — the
  // model's real context length). Preferred over the /models
  // effectiveCap(...) fallback from the first frame on; null before it (the
  // fallback then rules — see misc-slice.ts).
  context_window?: number | null;
  created_at: string;
  updated_at: string;
  // Emitted by serialize_record for every `*_at` field (pre-formatted for display).
  // Undeclared here until the wire-contract test found them.
  created_at_fmt?: string;
  updated_at_fmt?: string;
}

export interface ChatMessage {
  message_id: string;
  chat_id: string;
  parent_id: string | null;
  role: 'user' | 'assistant';
  content: string;
  // images holds base64 data URIs. list_messages OMITs them (heavy payload) and
  // sends image_count instead; the bubble lazy-fetches the URIs. Present (already
  // hydrated) on the create echo / optimistic sends.
  images?: string[];
  image_count?: number;
  model?: string;
  sources?: ChatSource[];
  unsaved?: boolean;
  // WHY: this field is snake_case because it IS the wire name — the chat frame
  // and the driver's replayed frames carry it verbatim, and a camelCase name
  // here would type-check fine while every tool chip silently rendered null
  // Still-held mutating calls of this message (mid-turn approval). Client-side
  // only — restored from GET /chat/verdicts after reload, appended by the
  // `lore/verdict-ask` mint while live.
  pending_verdicts?: PendingVerdict[];
  // ARCH: the abnormal-end halt
  // card, its own column (NOT a `segments` member anymore). The reason is the
  // backend's ABNORMAL-end vocabulary (`error`, `stream_failed`,
  // `turn_timeout`, `line_unreachable`, `disconnected`) — a STRING, wider than
   // the live budget-halt segment's typed `HaltReason` union (which the
   // persisted segment path already outgrew the same way — its declared
   // `HaltReason` was honest only on paper). The renderer casts to HaltCard's
   // prop; an unknown reason renders degraded-but-visible.
  halt?: { reason: string; steps?: number; limit?: number };
  // ARCH: the DETACHED generate_image run's chips (refiner + image), their own
  // row column — the same reason `halt` has one: the driver's log cannot produce
  // them. The generation finishes in a background task long after the tool call
  // returned, so the replayed frames hold the call and nothing else, and the
  // refined SD prompt lives nowhere else. On reload the backend mints them into
  // the row's frames as `lore/image-gen` (driver_frames `_image_gen_frame`);
  // live, the same step dicts mint the run's settled lore/image-gen chat
  // frame — the worker pushes it, the browser feeds it verbatim (see SYSTEM:
  // dsh-conversation).
  gen_steps?: AgentStep[];
  // The driver's replayed frames for this turn — the reload input of the
  // assembler (see SYSTEM: dsh-conversation). Present only on the wire:
  // loadMessagesFor feeds them to replaceWindowFromRows and strips the key, so
  // nothing downstream reads it.
  frames?: unknown[];
  // The messages GET marks the row
  // whose turn is STILL OPEN (the trailing no-end_seq turn, backend
  // _mark_open_turn). Present only on the wire — adoptOpenTurn consumes it and
  // stripFrames drops it with `frames`; the store never carries it.
  open_turn?: boolean;
  // The plugin's fold of the OPEN turn's live stream (dsh's
  // SessionAssistantStreamAccumulator snapshot) — the reload's streamed-text
  // baseline. Present only on the wire, on the open row beside `open_turn`:
  // adoptOpenTurn seats the transient tail from it and stripFrames drops it;
  // the store never carries it.
  assistant_stream?: unknown;
  author_id?: string;
  author_name?: string;
  created_at: string;
  // Emitted by serialize_record for every `*_at` field (pre-formatted for display).
  // Undeclared here until the wire-contract test found it.
  created_at_fmt?: string;
}

export interface ChatSource {
  kind: 'document' | 'reference';
  id: string;
  title: string;
  heading?: string;
  snippet: string;
  offset_start?: number;
  offset_end?: number;
  score: number;
  retrieved?: boolean;
}

export interface SemanticSearchHit {
  kind: 'document' | 'reference';
  parent_id: string;
  parent_title: string;
  heading: string | null;
  snippet: string;
  offset_start: number | null;
  offset_end: number | null;
  score: number;
}

export interface SemanticSearchResult {
  query: string;
  hits: SemanticSearchHit[];
}

/**
 * True when a reference has a non-empty body server-side.
 *
 * List-sourced refs carry `has_content` (metadata-only LIST); hydrated / single-GET /
 * POST refs carry `content` directly but no `has_content`. This unifies both so any
 * gating UI (send-to-agent, embed availability) works regardless of how the ref was
 * loaded. INVARIANT: prefer `has_content` when present (authoritative server signal),
 * else fall back to the loaded content. Why: has_content is the server's authoritative emptiness signal; content may be stale or lazy.
 */
export function hasReferenceContent(ref: Pick<Reference, 'has_content' | 'content'>): boolean {
  if (ref.has_content !== undefined) return ref.has_content;
  return !!(ref.content && ref.content.trim());
}
