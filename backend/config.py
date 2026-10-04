"""Lore backend configuration — all values from environment, no defaults."""
# SYSTEM: config — environment configuration, prompts, and agent configs
#
# ARCH: every env-backed constant below is declared ONCE via setting(...) —
# the call reads the env, registers the admin-settings spec and returns the
# value for the assignment. settings_registry holds the machinery; this module
# is the sole declaration site (order matters: a `fallback` fold reads a base
# declared above it).

import base64
import json
import os
from pathlib import Path

import instance_secret
from comfy_markers import validate_size, validate_workflow
from settings_registry import (
    ConfigError as ConfigError,  # re-export: `from config import ConfigError` callers stay valid
)
from settings_registry import (
    _require_env as _require_env,  # re-export (test seam: tests/backend/test_unit_db.py)
)
from settings_registry import _section, setting

# ─── Prompts ─────────────────────────────────────────────────────────────────
# Loaded from configs/prompts.json. Each key maps to an LLM system message template.
# Placeholders use Python str.format() syntax: {title}, {content}, {chunks}, {refs}.

_prompts_path = Path(__file__).parent / "configs" / "prompts.json"
_prompts: dict = json.loads(_prompts_path.read_text())

# prompts.json carries no chat context templates (context pins scope, not
# bodies — see routes/chat/context.py) and no agent system prompt: the live one
# is BOOTSTRAP_SYSTEM_PROMPT (backend/agent_config.py), layered with persona/
# rules/skills from the agent-config subtree.
PROMPT_RETRIEVAL_QUERY_REWRITE: str = _prompts["retrieval"]["query_rewrite_system"]

# ─── Release version ─────────────────────────────────────────────────────────

# The release tag (`vMAJOR.MINOR.PATCH`) the deploy stamped in — see the `release` skill.
# WHY not _require_env: the tag only exists in a deployed image; dev and CI run from a
# working tree with no deploy step, and crashing the backend there buys nothing. The
# fallback is a LOUD sentinel, never an empty string — an unversioned build must be
# identifiable in a bug report, not indistinguishable from a real release.
APP_VERSION = os.environ.get("APP_VERSION") or "dev"

# ─── Schema fingerprint guard (SYSTEM: schema-fingerprint) ──────────────────
# Severity dial for the boot-time stale-schema-restore guard, deliberately NOT
# crash-on-missing-config: this is a guard's severity, not critical data. Default
# ON (W2): recorded fields missing from the live schema with no migration having
# run is data-loss-grade drift (2026-06-30 / 2026-07-28) and refuses boot. `false`
# is the operator escape hatch — boot a wedged environment (e.g. a SurrealDB
# output-format change tripping a false positive) without a code push; the drift
# still logs LOUD.
# WHY env-only, no admin row: the escape hatch is needed exactly when the backend
# cannot boot, so an admin page could never reach it; the critical log names it.
SCHEMA_FINGERPRINT_FATAL = (os.environ.get("SCHEMA_FINGERPRINT_FATAL") or "true").lower() in (
    "1", "true", "yes", "on",
)

# ─── Auth ────────────────────────────────────────────────────────────────────

# The JWT signing key (SECRET_KEY, below) is deliberately NOT a setting — no
# env var, no admin row. It is generated per install at
# $STORAGE_PATH/.lore/secret_key (instance_secret), a NAMED exception to
# "secrets live in the secrets volume": the file predates secrets-init and
# moving it would rotate the key (logging out every session). It signs only
# session cookies and short MCP tokens. The cookie's Secure flag is not a
# setting either: it follows the browser's scheme (auth.set_session_cookie).
ALGORITHM = "HS256"  # JWT algorithm
COOKIE_MAX_AGE = 60 * 60 * 24 * 14  # Session cookie lifetime (14 days)

# ─── Storage ─────────────────────────────────────────────────────────────────

# INFRA WIRING, not configuration (plan component-wiring-not-settings): the
# storage root is the compose mount (/storage in every stack). The env read is
# the TEST seam — tests/backend/conftest.py:190 points each xdist worker at
# its own storage subtree; no compose, .env.example, deploy or README sets or
# mentions it.
STORAGE_PATH = Path(os.environ.get("STORAGE_PATH", "/storage"))
# "" = no env leg: the key exists only as the per-install file.
SECRET_KEY = instance_secret.resolve("", STORAGE_PATH)  # JWT signing key

_section("storage", "Storage")
MAX_AUDIO_SIZE_MB = setting(
    "MAX_AUDIO_SIZE_MB", int, default=500, min=1,
    label="Max audio upload, MB",
    help="Upload limit for audio references. The multipart pre-reject applies the "
         "boot value; the authoritative check is live.",
)  # Upload limit for audio
MAX_IMAGE_SIZE_MB = setting(
    "MAX_IMAGE_SIZE_MB", int, default=50, min=1,
    label="Max image upload, MB",
    help="Upload limit for image references.",
)  # Upload limit for images
MAX_MARKDOWN_SIZE_MB = setting(
    "MAX_MARKDOWN_SIZE_MB", int, default=50, min=1,
    label="Max markdown upload, MB",
    help="Upload limit for markdown/text files.",
)  # Upload limit for markdown files
MAX_DOCX_SIZE_MB = setting(
    "MAX_DOCX_SIZE_MB", int, default=50, min=1,
    label="Max .docx import, MB",
    help="Upload limit for .docx imports.",
)  # Upload limit for .docx imports
# Not shared with MAX_DOCX_SIZE_MB: the two formats have different memory
# profiles per stored megabyte (PDF keeps three simultaneous copies at the cap —
# web read, worker read, multipart buffer; see plan pdf-import-pymupdf4llm Risks).
MAX_PDF_SIZE_MB = setting(
    "MAX_PDF_SIZE_MB", int, default=50, min=1,
    label="Max .pdf import, MB",
    help="Upload limit for .pdf imports (kept separate from docx: different memory "
         "profile per stored megabyte).",
)  # Upload limit for .pdf imports
MAX_ARCHIVE_SIZE_MB = setting(
    "MAX_ARCHIVE_SIZE_MB", int, default=100, min=1,
    label="Max archive upload, MB",
    help="Upload limit for agent-shared .zip archives (import_file sandbox_path).",
)  # Upload limit for agent-shared .zip archives (import_file sandbox_path)

# DOCX→Markdown conversion is offloaded to the stateless `converter` container
# (Pandoc, later swappable for ML engines). Internal Docker-network address —
# wiring between Lore's own required components, not configuration (see the
# agent-line INVARIANT below for the why).
CONVERTER_URL = "http://converter:8002"
EXPORT_CONVERTER_TIMEOUT_S = 120  # DOCX/PDF export via the converter (slow; > WEB_CONVERTER_TIMEOUT)

# The MCP_* trio sits under config's Storage header but belongs to Tools — its
# registry section is "MCP gateway".
_section("tools", "MCP gateway")
# preview_extractor (the env var keeps the tool's older `RUN_EXTRACTOR` name) is a
# ~50s dev/benchmark tool that dry-runs the local LLM. Off the default MCP surface
# (a heavy tool advertised to every external client is a footgun). An explicitly-
# enabled deployment (the CIR benchmark work) sets MCP_RUN_EXTRACTOR=1 to surface it.
MCP_RUN_EXTRACTOR = setting(
    "MCP_RUN_EXTRACTOR", bool, default=False,
    label="Serve preview_extractor over MCP",
    help="Dry-run benchmark tool, off the default external-MCP surface (a "
         "~50s tool advertised to every client is a footgun).",
)
# TTL (seconds) on the signed download URL minted by the MCP get_file tool. Short
# by design — the token authorizes exactly one ref_id and expires; it is not a
# session and grants nothing else. 5 minutes is enough for a client to fetch after
# a tool call, short enough that a leaked URL stops working quickly.
MCP_DOWNLOAD_TOKEN_TTL_S = setting(
    "MCP_DOWNLOAD_TOKEN_TTL_S", int, default=300, min=1,
    label="get_file URL TTL, s",
    help="Lifetime of the signed one-ref_id download URL minted by the "
         "get_file tool.",
)
# TTL (seconds) on the signed UPLOAD URL minted by attach_file. Longer than the
# download TTL on purpose: the whole point is files the base64 path could
# not carry (hundreds of MB), and the clock covers the client's ENTIRE stream, not
# just the moment of a request. 15 min uploads ~500MB on a slow link while still
# expiring a leaked URL quickly. See INVARIANT in mcp_gateway/upload.py.
MCP_UPLOAD_TOKEN_TTL_S = setting(
    "MCP_UPLOAD_TOKEN_TTL_S", int, default=900, min=1,
    label="attach_file URL TTL, s",
    help="Lifetime of the signed upload URL — the clock covers the client's "
         "whole stream.",
)

# TTL (seconds) on a MEMORY consolidation run's scoped agent key
# (`mint_memory_run_key` → `mint_run_key`). Sub-day on purpose: the run key is a
# LIVE credential only for the run's lifetime, and the run's row IS the run — a
# 90-day row that is never reaped is accumulation, not a credential anyone can
# use (its plaintext dies at mint). 6 h matches the session run-pointer horizon
# (memory/run_key.MEM_RUN_TTL_S); beyond it the session has no pointer anyway.
# Mitigation for a run that outlives the TTL: a tool call then 401s loudly at the
# next auth (reads as a bug, but only on a run nobody resumed).
# A bare constant, not a setting: nobody meaningfully
# chose it — sizing runs longer is a release change, not a live knob.
MEM_RUN_KEY_TTL_S = 6 * 3600

AUDIO_MIMES = {
    "audio/webm",
    "video/webm",
    "audio/ogg",
    "audio/opus",
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
    "audio/aac",
    "audio/3gpp",
    "audio/3gpp2",
}  # Accepted audio MIME types (video/webm: browsers and file(1) report webm as video/*)
IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}  # Accepted image MIME types

# ─── AI API (base for STT + Chat, OpenAI-compatible) ────────────────────────
# ARCH: Single AI_API_URL/KEY base — STT and Chat share the same server.
# STT_ env vars override the base for per-service flexibility.

_section("models", "AI API")
AI_API_URL = setting(
    "AI_API_URL", str, default="",
    label="AI API base URL",
    help="The one OpenAI-compatible base URL STT and Chat both default to.",
)
AI_API_KEY = setting(
    "AI_API_KEY", "secret", default="",
    label="AI API base key",
    help="The shared key of the AI API base.",
)
CHAT_MODEL = setting(
    "CHAT_MODEL", str, default="",
    label="Default chat model",
    help="The model a new session pins when nothing else selects one.",
)
CHAT_TITLE_MODEL = setting(
    "CHAT_TITLE_MODEL", str, fallback=("CHAT_MODEL",),
    label="Session title model",
    help="The model the agent names chat sessions with; empty = the default "
         "chat model. Use a non-reasoning model — a reasoning one thinks on "
         "every title (the harness log warns). Applies from the next message.",
)

# The STT overrides keep their own admin sub-block (STT section) under this
# header: the operator reads the base pair, then the transcription overrides.
_section("models", "STT")
STT_API_URL = setting(
    "STT_API_URL", str, fallback=("AI_API_URL",),
    label="STT API URL",
    help="Transcription endpoint; empty = fall back to the AI API base URL.",
)
STT_API_KEY = setting(
    "STT_API_KEY", "secret", fallback=("AI_API_KEY",),
    label="STT API key",
    help="Transcription key; empty = fall back to the AI API base key.",
)
STT_MODEL = setting(
    "STT_MODEL", str, default="whisper-1",
    label="STT model",
    help="Model name sent to the transcription endpoint (whisper-1 format).",
)

# Session TITLES are minted by the harness titler; CHAT_TITLE_MODEL above only
# names its model, served to the harness per turn (the turn payload).
# ARCH: Total budget for ALL chat image attachments in one turn, in raw source
# bytes (not base64). The frontend enforces this sum at attach time;
# main.py derives the request-body JSON cap from it. Single source of truth —
# exposed to clients via GET /chat/models (max_attachment_mb).
CHAT_MAX_IMAGE_SIZE_MB = 5

# ─── Auto-backup thresholds ─────────────────────────────────────────────────

BACKUP_MIN_CONTENT = 200  # Don't backup documents shorter than this
BACKUP_DANGEROUS_RATIO = 0.30  # 30% of baseline content lost
BACKUP_DANGEROUS_ABSOLUTE = 500  # OR 500+ chars lost
HANDOFF_MIN_CONTENT = 50  # Minimum content length for editor-handoff backup
BACKUP_DIFF_CAP = (
    50_000  # Max chars for difflib; above this, fall back to span heuristic
)
# WHY: difflib is O(n*m) and runs inline on the web event loop
# via the REST PATCH path — blocking the loop for seconds is
# unacceptable on large docs. 50K covers typical prose documents.

_section("storage", "Auto-backup thresholds")
THIN_RECENT_HOURS = setting(
    "THIN_RECENT_HOURS", int, default=48, min=1,
    label="Thinning: recent hours",
    help="Checkpoints younger than this are never thinned.",
)
THIN_DAILY_KEEP_DAYS = setting(
    "THIN_DAILY_KEEP_DAYS", int, default=30, min=0,
    label="Thinning: daily keep, days",
    help="Daily-granularity retention window after the recent hours.",
)
THIN_WEEKLY_KEEP_WEEKS = setting(
    "THIN_WEEKLY_KEEP_WEEKS", int, default=12, min=0,
    label="Thinning: weekly keep, weeks",
    help="Weekly-granularity retention window after the daily one.",
)
# Hour (UTC) at which the daily GFS thinning cron runs. Low-traffic window by default.
THIN_CRON_HOUR = setting(
    "THIN_CRON_HOUR", int, default=3, min=0, max=23, effect="restart",
    label="Thinning cron hour (UTC)",
    help="Hour of the daily thinning/retention cron. Set in .env — the cron "
         "schedule is frozen at worker boot.",
)

# ─── CRDT / Backplane / Job Queue ────────────────────────────────────────────

# INFRA WIRING, not configuration (plan component-wiring-not-settings): the
# Redis backing jobs, the backplane and caches is the compose service. The env
# read is the TEST seam — tests/backend/conftest.py:201 isolates each xdist
# worker on Redis db 15-N (the autouse fixture FLUSHDBs it); a bare constant
# here would point the suite at the LIVE db 0 and wipe it. No compose,
# .env.example, deploy or README sets or mentions it.
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

# ─── Database ────────────────────────────────────────────────────────────────
#
# INFRA WIRING, not configuration (plan component-wiring-not-settings): the
# database is the compose service — the address and the sign-in user are
# constants, and the password exists only as the file secrets-init generates
# into the `secrets` volume (SURREAL_PASS_FILE; read at call time by
# db.pool.surreal_password, never an env var). The env reads on the
# namespace/database are the TEST seam — tests/backend/conftest.py:32 and :50
# point the suite at the lore_test namespace and the per-worker test_gwN
# databases; a bare constant would land the suite's per-test DELETE cleanup
# on the LIVE lore/main and wipe it. No compose, .env.example, deploy or
# README sets or mentions either name.
SURREAL_URL = "ws://surreal:8000/rpc"
SURREAL_USER = "root"
SURREAL_NS = os.environ.get("SURREAL_NS", "lore")
SURREAL_DB = os.environ.get("SURREAL_DB", "main")
SURREAL_PASS_FILE = Path("/secrets/surreal/pass")
# Read-only view of the surreal data volume, mounted into the backend by all
# three compose files (dev binds ./data/surreal; prod/public mount the volume).
# admin_info stats the FILES under it (GET /api/admin/info/storage); it never
# reads their contents. INFRA WIRING, not configuration — the path is fixed by
# compose, like SURREAL_URL above; no env carries it.
SURREAL_DB_DIR = Path("/surreal-data/lore.db")

# Shown with the rest of STT in the models tab, where an operator looks for it.
_section("models", "STT")
STT_CONCURRENCY = setting(
    "STT_CONCURRENCY", int, default=2, min=1, effect="restart",
    label="STT concurrency",
    help="In-flight transcription jobs (transcription worker max_jobs). Set in "
         ".env — the worker pool is sized at boot.",
)
# ─── CRDT / Backplane / Job Queue ────────────────────────────────────────────
# Backplane pub/sub publish deadline (seconds). Why: pub/sub fan-out is
# fire-and-forget — every caller (publish_doc_update, event_bus._publish_to_backplane)
# already wraps bp.publish in try/except and treats a failure as a tolerated
# best-effort miss. An UNBOUNDED await here hangs forever on the DinD CI runner
# when the per-loop Redis connection wedges, and pytest-timeout then kills the
# whole suite (120s, no traceback) at whatever test boundary it lands on. Bounding
# it turns a hang into the already-handled TimeoutError → logged warning path.
# A bare constant, not a setting.
BACKPLANE_PUBLISH_TIMEOUT_S = 5.0
# WHY: Redis mandatory; Redis down = service down. No in-process fallback.  Why: the collab backplane + arq job queue both depend on Redis; an in-process fallback would silently drop cross-replica broadcasts/jobs, so a Redis outage is a hard failure, not a degraded one.
# ARCH: Single Redis instance serves both backplane pub/sub and arq job queue —
# disjoint keyspaces (ydoc:/evt: channels vs arq: keys), no collision.

# ─── Collab limits ───────────────────────────────────────────────────────────

BATCH_INTERVAL = 0.05  # Broadcast batching window (seconds)
FLUSH_INTERVAL_SEC = 1  # Periodic flush-loop tick (s); also zombie-client reap cadence
# WHY: minimum spacing between SNAPSHOT DB writes on the periodic path. Why:
# each flush rewrites the FULL documents row (content + ydoc_state binary) — for a
# large doc (0.6MB+) a 1s cadence is ~0.5–1MB/s of KV writes → 20x store
# amplification over days → surreal OOM kill loop (2026-08-12 and 2026-08-15,
# RestartCount 393/9, store 4.2–4.4GB vs ~200MB logical). Durability is NOT
# affected: the append-only ydoc_updates log is the floor (see flush_pipeline.py
# INVARIANT(data-loss)); this only paces the derived snapshot. The gate advances
# on SUCCESSFUL flushes only (FlushPipeline._snapshot_pace_ok) — failed flushes
# keep the 1s retry tick so save_degraded signalling stays fast. The FIRST edit
# of a session flushes immediately (leading edge). Force paths
# (close/leave/handoff/shutdown) bypass pacing entirely.
# A bare constant, not a setting.
FLUSH_SNAPSHOT_MIN_INTERVAL_SEC = 15.0

# ─── Chat constants ────────────────────────────────────────────────────────────

CHAT_LLM_TIMEOUT_S = 300  # LLM streaming timeout (seconds)
CHAT_MODELS_TIMEOUT_S = 10  # HTTP timeout for the /models gateway probe
# Per-model capability numbers (context window, output cap, vision) are NOT
# configured here — the DRIVER resolves them off the gateway /v1/models
# (plugin caps.ts — plan collapse-the-editor-harness-layer step 4). The named
# fallbacks are the composition's own: defaultContextWindow 128000 (the
# plugin's ROUTE_DEFAULT_CONTEXT_WINDOW in harness-driver/plugin/src/index.ts)
# and dsh's default maxTokens when a gateway entry omits the ceiling. The
# frontend gauge fallback (CHAT_CONTEXT_WINDOW_FALLBACK in misc-slice.ts) is
# the client's own constant for models the picker map leaves bare.
# TTL for the cached gateway /v1/models fetch behind GET /models (the picker
# snapshot; the turn-time gates do not read it).
# A bare constant, not a setting.
CHAT_MODELS_CACHE_TTL_S = 60.0
CHAT_ERROR_TRUNCATE_CHARS = 200  # Max chars to show from error messages
CHAT_CONTEXT_SNIPPET_LEN = 300  # Max chars for context snippet in prompts
MAX_PROPOSAL_NEW_TEXT_CHARS = (
    100_000  # Hard cap for agent edit_document new_string (aliased to AGENT_EDIT_MAX_CHARS)
)

# ─── Model-bound image normalization (SYSTEM: thumbnails) ─────────────────────
# Images are normalized on the way to the model: pixel area capped + format
# restricted to the set the target model actually decodes. Measured against the
# live dev endpoint (nnp-llm-router), not vendor docs (source: the retired
# probe-vision.py — its method survives in git history).
# INVARIANT: WebP never reaches the model. Why: on both local Qwen3.6 models a
# WebP part is silently dropped (23 prompt tokens = text-only level) and the
# model hallucinates a confident wrong answer about an image it never saw. The
# output safe set is therefore {PNG, JPEG} regardless of source format.
MODEL_IMAGE_SAFE_MIMES = frozenset({"image/png", "image/jpeg"})
# WHY: cost is a pure function of pixel AREA, saturating flat at ~1.25 Mpx.
# Why: measured linear cost up to ~1.25 Mpx, then a flat ~1100-token ceiling
# independent of aspect ratio (2576x2576 and 1400x1400 both bill 1112). So we cap
# total pixels — scaling both sides by sqrt(cap/area) — never the long edge (that
# lets 3000x500 through at 1.5 Mpx while needlessly shrinking 1200x1200), and
# never upscale.
# What the cap buys: ~4x smaller payload/base64 (25.9KB -> 6.4KB at 2576x2576),
# lower latency, and a deterministic resize so the data-URI bytes are stable and
# the prompt cache can hit.
# What it does NOT buy — do not re-add this claim: better legibility. The server
# downscales to this same budget itself, so text too small to survive our LANCZOS
# was already lost. Measured on a rendered size label at 2576x2576: raw read back
# "257x1255", normalized read back "39578x5378" — both wrong, identical token
# cost. Small text in a full-page scan needs tiling/crops, not a different cap.
# Swapping the model behind the endpoint moves this number — re-measure against the
# endpoint (the retired probe-vision method: send images, compare billed tokens
# across pixel areas) and adjust here.
_section("models", "Images for the model")
MODEL_IMAGE_MAX_PIXELS = setting(
    "MODEL_IMAGE_MAX_PIXELS", int, default=1_250_000, min=10_000,
    label="Model image pixel cap",
    help="Total-pixel cap for images normalized on the way to the model "
         "(cost is a function of pixel area).",
)
MODEL_IMAGE_JPEG_QUALITY = setting(
    "MODEL_IMAGE_JPEG_QUALITY", int, default=85, min=1, max=100,
    label="Model image JPEG quality",
    help="JPEG quality for the model-bound image variant.",
)
# Which models accept image_url parts is NOT configured here: the gateway reports
# it per model, the DRIVER resolves it (plugin caps.ts), and the backend's vision
# gate reads the driver's reply (driver.client.agent_capability).


# ─── Agent line ──────────────────────────────────────────────────────────────
# ARCH: the agent line (harness) is the brain and the ONLY AI line — there is no
# in-process multi-tool agent loop, no zero-tools Ask line and no `mode` axis. Every
# AI chat is an agent chat, so there are no in-process loop bounds to configure here
# either.
# WHY single line: the dual-line hedge is gone — resolver branch, service and
# globals deleted, its chats archived read-only.
_section("agent", "Agent line")
# WHY: edits that cover >= this fraction of the document are rejected as
# full rewrites. The model is directed to create_document instead.  Why: a single edit covering most of the document is a wholesale replacement, not a patch; rejecting it forces create_document and keeps the apply path to bounded surgical edits.
AGENT_FULL_REWRITE_FRACTION = setting(
    "AGENT_FULL_REWRITE_FRACTION", float, default=0.9, min=0.1, max=1.0,
    label="Full-rewrite ban fraction",
    help="An edit_document old_string covering >= this fraction of the document "
         "is rejected as a wholesale replacement (create_document is the escape "
         "hatch).",
)
# Code-point length cap on edit_document old_string/new_string (mirrors the
# replace_selected_fragment guard so a runaway tool arg can't blow up Surreal).
AGENT_EDIT_MAX_CHARS = MAX_PROPOSAL_NEW_TEXT_CHARS
# Read-path spill bound: read_document
# returns `content` as an offset/limit slice, never the unbounded buffer.
# Default sized so a typical document still arrives in ONE call (~7.5K tokens);
# the self-describing next_offset metadata keeps paging an agent-driven
# decision instead of silent truncation.
# A bare constant, not a setting: the slice window is
# sized against AGENT_READ_MAX_CHARS, not chosen by an operator.
AGENT_READ_SLICE_CHARS = 30_000
# Hard ceiling for an explicit `limit`, DERIVED from AGENT_EDIT_MAX_CHARS (not a
# copied literal, which would drift): one read may fetch at most what one edit
# may write.
AGENT_READ_MAX_CHARS = setting(
    "AGENT_READ_MAX_CHARS", int, default=AGENT_EDIT_MAX_CHARS, min=1000,
    label="read_document hard ceiling, chars",
    help="Hard cap on an explicit read_document `limit`.",
)
# Caps on agent table-tool dimensions (create_table / add_table_rows /
# add_table_column + the batch edit_table_cell). A single mutating call must not mint
# an unbounded CRDT model — each cell is a Y.Map built inside one doc.transaction(),
# so rows×cols×cell-size bounds the broadcast + persistence cost. No-silent-degradation:
# over-budget is rejected with 400 before the model is built.
AGENT_TABLE_MAX_ROWS = setting(
    "AGENT_TABLE_MAX_ROWS", int, default=500, min=1, effect="restart",
    label="Table rows cap (schema)",
    help="Max rows per table tool call. Set in .env — the tool schemas bake "
         "the caps at import.",
)
AGENT_TABLE_MAX_CELL_CHARS = setting(
    "AGENT_TABLE_MAX_CELL_CHARS", int, default=10_000, min=1, effect="restart",
    label="Table cell cap, chars (schema)",
    help="Max chars per table cell. Set in .env — the tool schemas bake the caps "
         "at import.",
)
# Direct (non-semantic) title/substring match layer count for search_materials.
AGENT_SEARCH_DIRECT_K = setting(
    "AGENT_SEARCH_DIRECT_K", int, default=5, min=0,
    label="Direct search layer, hits",
    help="Title/substring match layer count for search_materials (fused with the "
         "semantic layer).",
)
# Max chars of context document text injected into the agent's system prompt.
# Conservative (~20K tokens at 4 chars/tok) — leaves room for config prompt + conversation.
CHAT_MAX_AGENT_CONTEXT_CHARS = 80_000

# ─── Agent line — the rented harness (THE line) ──────────────────────────────
# Address + secret of the harness-driver service. This is the ONE line: chats
# pinned to the earlier, retired line are archived read-only, and every NEW
# session pins this line (`DRIVER_LINE_NAME`, driver/client.py).
# Unset secret ⇒ the agent line is explicitly unavailable (no silent fallback).
# INVARIANT(security): the driver address and secret are not configuration.
# Why: nobody ever changes them, and every way to set one (env, the admin panel)
# was a way to make the two sides disagree — a mismatch kills the agent behind a
# 401 (a v0.20.3 install whose admin typed a secret stays dead across every
# upgrade). So the address is the compose service name, and the secret exists
# only as the file secrets-init generates per install into the volume the
# backend and the harness both mount: no env var, no admin key, no override.
# The secret cannot be hardcoded or dropped: /api/ is public, and it is what
# proves a caller is OUR harness — the only caller whose "the user approved" is
# honored (agent/context.py driver_attested) and the only one /api/driver/*
# hands boot credentials to.
_section("agent", "Agent line — the rented harness")
HARNESS_DRIVER_URL = "http://harness:8090"
#: The generated per-install secret (secrets-init → the `secrets` volume,
#: mounted at /secrets). Read at IMPORT: "" (no file) ⇒ the line is
#: UNCONFIGURED — the routing gate's explicit-unavailable signal. Readers use
#: `config.HARNESS_DRIVER_SECRET` at call time; nothing but this file binds it.
_DRIVER_SECRET_FILE = Path("/secrets/harness/driver_secret")
HARNESS_DRIVER_SECRET = (
    _DRIVER_SECRET_FILE.read_text().strip()
    if _DRIVER_SECRET_FILE.is_file() else ""
)
# Availability signal: Redis-cached probe of the driver /health
# endpoint. Positive TTL (ok) and negative TTL (down) decouple frontend Agent-mode
# gating from per-request probing — the capability flag is served from cache.
# Decision 11 — per-session turn lock. A real Redis SET NX EX (NOT selection_conflict.py,
# which is a best-effort UX pre-check for cursor-region edits) guards one active
# turn per chat_session.
# WHY: the TTL is SHORT and a heartbeat
# (TURN_LOCK_HEARTBEAT_S) refreshes it while the turn streams, so a leaked lock
# (a cancelled release / a process kill mid-turn) costs one heartbeat interval
# instead of minutes. This REPLACES the old "TTL slightly above TURN_PROGRESS_GRACE_S"
# reasoning: a legit-but-slow turn is kept alive by the heartbeat (compare-and-
# extend on each beat), NOT by a long TTL — so the TTL can be small (fast
# self-heal) without evicting slow turns. The release is ALSO shielded in
# completions (defence in depth); this short TTL is the backstop for the
# process-kill case where even the shield cannot run.
# A bare constant, not a setting: the TTL is sized
# against TURN_LOCK_HEARTBEAT_S (>= 2 beats inside it), not chosen live.
TURN_LOCK_TTL_S = 30
# Heartbeat cadence: the relay/stream task refreshes the holder's lock TTL on this
# interval, independent of frame emission (a turn can emit no frames for tens of
# seconds during a single tool call). Sized so >=2 beats land inside the TTL.
# A bare constant with the TTL above.
TURN_LOCK_HEARTBEAT_S = 10.0
# Agent emergency stop, enforced as a HOLD-PAUSED, progress-extended deadline
# in the channel (driver.channel._HoldPausedDeadline): every real driver frame
# re-arms the no-progress window to now + TURN_PROGRESS_GRACE_S, clamped at the
# absolute wall TURN_MAX_WALL_S. A turn that is demonstrably working is never
# killed by the wall clock — only a silent one is, one grace window after its
# last frame. Env-driven so a runaway turn self-terminates without depending
# on a human pressing stop.
# The grace judges a channel that is alive but producing nothing useful (a
# genuinely dead socket is a RECONNECT, not a breach).
# The re-armed no-progress window (s): the budget for ONE slow-but-legitimate
# stretch of the turn — a single tool call can run tens of seconds emitting no
# frames. Armed at turn start, re-armed by every relayed frame.
TURN_PROGRESS_GRACE_S = setting(
    "TURN_PROGRESS_GRACE_S", float, default=300.0, min=1,
    label="No-progress grace, s",
    help="Silence budget of a turn: re-armed by every driver frame; a turn with "
         "no frames for this long is stopped.",
)
# The absolute ceiling (s) from turn start that survives progress: past it a
# breach fires whatever the driver is doing — without it a tool-loop that
# keeps emitting frames would never terminate. Held time is excluded (an
# approval pause moves this ceiling like the deadline); worst-case wall =
# TURN_MAX_WALL_S + TURN_HOLD_MAX_S.
TURN_MAX_WALL_S = setting(
    "TURN_MAX_WALL_S", float, default=1800.0, min=1,
    label="Turn hard wall, s",
    help="Absolute ceiling from turn start; even a progress-making turn breaches "
         "at it.",
)
# The pause cap for one held turn: the driver's approval bound mirrors this
# (LORE_APPROVAL_MAX_S on the harness — compose defaults keep the pair equal;
# the coupling is compose defaults plus this comment). Also the ceiling the
# turn-lock heartbeat is sized against.
TURN_HOLD_MAX_S = setting(
    "TURN_HOLD_MAX_S", float, default=600.0, min=1,
    label="Approval hold cap, s",
    help="Aggregate pause budget while a mid-turn approval waits for the "
         "user.",
)

# ─── Embeddings (OpenAI-compatible, optional — lazy validation at first use) ──

_section("models", "Embeddings")
EMBEDDING_API_URL = setting(
    "EMBEDDING_API_URL", str, fallback=("AI_API_URL",),
    label="Embedding API URL",
    help="Embedding provider endpoint; empty = fall back to the AI API base "
         "URL. Empty and unconfigured = embeddings off (lazy validation).",
)  # Embedding provider URL; falls back to AI_API_URL
EMBEDDING_API_KEY = setting(
    "EMBEDDING_API_KEY", "secret", fallback=("AI_API_KEY",),
    label="Embedding API key",
    help="Embedding provider key; empty = fall back to the AI API base key.",
)  # Embedding API key; falls back to AI_API_KEY
# WHY: a model change moves the three absolute cosine cuts WITH it, in the
# same commit — RETRIEVAL_MIN_SCORE, MEMORY_MERGE_CANDIDATE_MIN_SCORE,
# MEMORY_DUPLICATE_FACT_THRESHOLD. Why: each is a point on THIS model's score
# distribution, not a property of the corpus, and the distributions are SHAPED
# differently: giga/480m's background scores sit lower than qwen3/600m's (fact-vs-fact
# null median 0.601 -> 0.464, retrieval hits likewise) while near-duplicate pairs score
# about the same (a template pair differing by one word: 0.844 qwen3, 0.858 giga). So a
# cut copied across models is a different operating point — qwen3's 0.35 under giga
# dropped 10 of 20 correct retrieval hits and read as "the new model is worse". Pinned
# by test_embedding_calibration_set_moves_together.
EMBEDDING_MODEL = setting(
    "EMBEDDING_MODEL", str, default="embeddings/giga/480m",
    label="Embedding model",
    help="Embedding model name. Changing it marks every stored vector stale "
         "(the chunk hash mixes the model name) and OBLIGES re-calibrating the "
         "three cosine cuts (see config.py); the hourly sweep re-embeds a "
         "capped batch per run — Admin → Embeddings → embed missing does all "
         "at once.",
)  # Embedding model name
EMBEDDING_BATCH_SIZE = setting(
    "EMBEDDING_BATCH_SIZE", int, default=32, min=1,
    label="Embedding batch size",
    help="Texts per embedding API call.",
)  # Texts per embedding API call
EMBEDDING_CONCURRENCY = setting(
    "EMBEDDING_CONCURRENCY", int, default=4, min=1, effect="restart",
    label="Embedding concurrency",
    help="Parallel embedding requests. Set in .env — the semaphore is built "
         "once at import.",
)  # Parallel embedding requests
EMBEDDING_COOLDOWN_SEC = setting(
    "EMBEDDING_COOLDOWN_SEC", int, default=3600, min=1,
    label="Re-embed debounce, s",
    help="Idle window after the last edit before a document re-embeds.",
)  # Seconds between idle embedding re-checks
EMBEDDING_TIMEOUT_S = 60  # HTTP timeout for a single embedding API batch request

# ─── Web search: provider for the harness-served web_search tool ─────────────
# ARCH: Lore owns these values, the harness owns search. The resolved provider
# and its ONE credential ride EVERY turn payload (driver.client), and the
# harness applies them with dsh's own calls at the turn's start — an admin
# change reaches the next message, no restart. There is NO off state: the
# web_search tool is always offered, and a provider with an empty key/URL
# fails every search with an explicit error naming the provider and this
# panel. To stop searching, clear the key/URL.
_section("search", "Web search")
# WHY default deepseek and no "off": an unpinned provider makes dsh pick any
# usable one — DeepSeek with a key resolver is always usable, so "off" spelled
# as an unpinned provider is a silent fallback, never an off switch; the
# operator's rule is that clearing the key is the off switch, and a fresh
# install's empty DeepSeek key fails loudly instead.
WEB_SEARCH_PROVIDER = setting(
    "WEB_SEARCH_PROVIDER", str, default="deepseek",
    choices=("deepseek", "brave", "tavily", "searxng"),
    label="Search provider",
    help="The one provider behind the agent's web_search tool; it is always "
         "offered — a provider with an empty key/URL fails every search "
         "with an explicit error instead of falling back (to stop searching, "
         "clear the key/URL below). DeepSeek search is a paid model call per "
         "query. Limits (60s timeout, 8 results, 4 queries per call) are "
         "fixed in the harness config. Applies from the next message.",
)
DEEPSEEK_API_KEY = setting(
    "DEEPSEEK_API_KEY", "secret", default="",
    visible_if=("WEB_SEARCH_PROVIDER", "deepseek"),
    label="DeepSeek API key",
    help="Used when the provider is deepseek. Reset falls back to .env. "
         "Applies from the next message.",
)
BRAVE_API_KEY = setting(
    "BRAVE_API_KEY", "secret", default="",
    visible_if=("WEB_SEARCH_PROVIDER", "brave"),
    label="Brave Search API key",
    help="Used when the provider is brave. Applies from the next message.",
)
TAVILY_API_KEY = setting(
    "TAVILY_API_KEY", "secret", default="",
    visible_if=("WEB_SEARCH_PROVIDER", "tavily"),
    label="Tavily API key",
    help="Used when the provider is tavily. Applies from the next message.",
)
SEARXNG_URL = setting(
    "SEARXNG_URL", str, default="",
    visible_if=("WEB_SEARCH_PROVIDER", "searxng"),
    label="SearXNG URL",
    help="Base URL of a SearXNG instance with the JSON format enabled; used "
         "when the provider is searxng. Applies from the next message.",
)


# ─── Retrieval ────────────────────────────────────────────────────────────────
# RAG pipeline: vector search → anti-monopoly → score drop-off → token budgeting.
# All retrieval_* constants control the search quality/quantity tradeoff.

RETRIEVAL_TOP_K_DOCS = 8  # Max document chunks returned by vector search
RETRIEVAL_TOP_K_REFS = 8  # Max reference chunks returned by vector search
RETRIEVAL_MAX_PER_DOC = 2  # Max chunks per single document (anti-monopoly)
RETRIEVAL_BUDGET_TOKENS_DOCS = 8000  # Token budget for document context injection
RETRIEVAL_BUDGET_TOKENS_REFS = 4000  # Token budget for reference context injection
RETRIEVAL_TOP_K_MEMORY = 8  # Max memory fact-docs returned by vector search
# Smaller than the docs budget on purpose: a fact body IS the distilled fact, so a
# memory hit buys far more per token than a raw chunk — and the
# budget is what stops a large Memory folder from evicting the raw material the answer
# still has to be grounded in.
RETRIEVAL_BUDGET_TOKENS_MEMORY = 3000
RETRIEVAL_CHUNK_MAX_CHARS = 3000  # Soft target: max chars per chunk during indexing
# Hard ceiling the chunker can NEVER exceed — provider rejects oversized input strings
# (measured on dev: 12k OK, 20k → HTTP 400). Well under the safe boundary so token
# variance across content cannot trip it. Only the final character-split fallback for
# pathological no-separator input (e.g. a base64 blob) reaches this high.
EMBEDDING_INPUT_MAX_CHARS = 8000
# Qwen3-Embedding is asymmetric: documents are embedded plain, queries are prefixed
# with an instruction (`Instruct: …\nQuery: …`). Omitting it costs 1–5% retrieval per
# the model card. Applied OPT-IN at the one query call site (retrieval.py), never as a
# default inside embed_texts (would poison every stored document vector).
_section("search", "Retrieval")
RETRIEVAL_QUERY_INSTRUCTION = setting(
    "RETRIEVAL_QUERY_INSTRUCTION", str,
    default="Given a user query, retrieve relevant document passages that answer it",
    label="Query-side embedding instruction",
    help="Instruction prefixed to retrieval queries (Qwen3-Embedding asymmetric "
         "format); documents are embedded plain.",
)
# WHY: 0.18 is calibrated against embeddings/giga/480m and must be re-derived
# on any EMBEDDING_MODEL change. Why: it is the all-hits median of that model's
# correct-answer scores on the 20-query hard set — the same place 0.35 sat for
# qwen3/600m. At 0.35 giga loses 10 of 20 correct hits (the `section` answers it
# wins included) while qwen3 loses none; at 0.18 it keeps 20 of 20.
RETRIEVAL_MIN_SCORE = setting(
    "RETRIEVAL_MIN_SCORE", float, default=0.18, min=0.0, max=1.0,
    label="Retrieval score floor",
    help="Minimum cosine similarity for a chunk hit to be included "
         "(calibrated per embedding model — see config.py before touching).",
)  # Minimum cosine similarity to include a hit
# WHY: RETRIEVAL_SCORE_DROP_OFF uses relative ratio, not absolute — a hit scoring 65% of the
# previous one indicates a relevance cliff, catching topic transitions absolute thresholds miss.
RETRIEVAL_SCORE_DROP_OFF = setting(
    "RETRIEVAL_SCORE_DROP_OFF", float, default=0.65, min=0.0, max=1.0,
    label="Retrieval drop-off ratio",
    help="A hit scoring below this ratio of the previous one is a relevance "
         "cliff — everything after it is cut.",
)  # Ratio threshold: hits[i].score < hits[i-1].score * this → cutoff
RETRIEVAL_TOKEN_SAFETY_MARGIN = 0.85  # Approx token ratio: chars/4 * margin


# ─── Query rewrite ────────────────────────────────────────────────────────────

_section("search", "Query rewrite")
CHAT_QUERY_REWRITE_MODEL = setting(
    "CHAT_QUERY_REWRITE_MODEL", str, fallback=("CHAT_MODEL",),
    label="Query rewrite model",
    help="LLM that rewrites a conversational query into a standalone one; "
         "empty = the default chat model.",
)  # LLM model for query rewriting; falls back to CHAT_MODEL
CHAT_QUERY_REWRITE_MAX_TOKENS = 100  # Max output tokens for rewritten query
CHAT_QUERY_REWRITE_ENABLED = setting(
    "CHAT_QUERY_REWRITE_ENABLED", bool, default=True,
    label="Query rewrite on/off",
    help="Off = retrieval embeds the raw user message (no LLM hop).",
)  # Toggle LLM query rewriting on/off
CHAT_QUERY_REWRITE_HISTORY_MESSAGES = setting(
    "CHAT_QUERY_REWRITE_HISTORY_MESSAGES", int, default=6, min=0,
    label="Rewrite history messages",
    help="How many recent chat messages the rewriter sees.",
)  # Number of history messages fed to rewriter
CHAT_QUERY_REWRITE_SNIPPET_CHARS = setting(
    "CHAT_QUERY_REWRITE_SNIPPET_CHARS", int, default=200, min=1,
    label="Rewrite snippet chars",
    help="Char cap per history message fed to the rewriter.",
)  # Char limit per history message snippet
CHAT_QUERY_REWRITE_TIMEOUT_S = setting(
    "CHAT_QUERY_REWRITE_TIMEOUT_S", int, default=15, min=1,
    label="Rewrite LLM timeout, s",
    help="HTTP timeout of the rewrite call; on timeout the raw query is "
         "used.",
)  # HTTP timeout for rewrite LLM call

# ─── Project memory (SYSTEM: memory) ─────────────────────────────────────────
# Order 2 (`refactor(memory): collapse the model to one fact level`) removed the
# entity level: a fact is a document, there is no `mem_entity_type`, and the closed
# type list that gated merges across it is gone (D6 — the type flips were unstable
# run-to-run and a closed list nobody applies consistently is a coin flip that splits
# subjects, not a guard).
# The verdict-batch ceiling — a TRUNCATION GUARD, not a batch-size target.
# The unit of consolidation is the REFERENCE: a run extracts
# everything a reference carries and applies it in batches that FIT the driver's output
# cap. This number is the guard that
# catches a batch too large to emit in one call — without it, an oversized batch is not
# refused (a clean rejection with the list to recover from) but truncates mid-JSON and
# the call never arrives, a silent dead loop. So it is set WELL ABOVE any plausible
# single-reference batch (a full reference yields low tens of facts) yet below the
# ~290-verdict line where 32k tokens of Cyrillic BPE would truncate — i.e. a safety net,
# never read as "aim for this many". Enforced as `maxItems` in
# ToolApplyMemoryVerdicts and NEVER advertised as a target in the skill or the tool
# description (testing.md: derive, don't copy).
MEMORY_VERDICTS_MAX_PER_BATCH = 100

# The payload budget for get_memory_facts. This is its
# OWN budget — RETRIEVAL_BUDGET_TOKENS_MEMORY must NOT be reused here: that constant
# governs the stage-5 semantic-injection path where the server chooses what to
# surface, while get_memory_facts is an explicit id-list fetch (the agent named
# exactly these facts); silently budgeting a direct request would drop what was
# explicitly asked for — a different contract, a different knob.
MEMORY_FACTS_PAGE_CHARS = 48_000  # total chars ceiling per fetch payload

# The portion payload's `merge_candidates` — how many
# nearest-neighbour memory facts to surface, and the minimum cosine below which a
# fact is NOT a candidate (an orthogonal fact is noise, not a hint). The cap keeps
# the payload bounded; the floor keeps the list signal, not padding.
MEMORY_MERGE_CANDIDATES = 8
# WHY: 0.25 is calibrated against embeddings/giga/480m and must be re-derived
# on any EMBEDDING_MODEL change. Why: it is qwen3's 0.35 scaled by the shift in the
# fact-vs-fact null median (0.601 -> 0.464 across the two models); a loose floor,
# so the lowest-risk of the three cuts, but still a point on the model's distribution.
MEMORY_MERGE_CANDIDATE_MIN_SCORE = 0.25
MEMORY_MERGE_CANDIDATE_QUERY_CHARS = 2000  # bounded prefix of the reference embed
# The char ceiling for the FACT BODIES that ride along with those candidates. Its own
# knob, and deliberately small: a candidate's body is served as `{id, text}` only,
# because that is all sameness is judged on. At the measured ~600 chars of fact text
# per fact, 8 candidates cost a bounded slice of a portion, whose bulk is the
# transcript itself. Raising this trades the one thing the incremental run exists to
# protect.
MEMORY_MERGE_CANDIDATE_CHARS = 8000

# The cosine at or above which a `new` fact is refused as restating a stored one
# (SYSTEM: memory, memory/dedup.py).
# WHY: 0.82 is calibrated against embeddings/giga/480m and must be re-derived
# on any EMBEDDING_MODEL change. Why: it is the equal-recall point of qwen3's 0.89 —
# 0.89 recalled 21.8% of known duplicates under qwen3 and only 7.9% under giga; 0.82
# restores 21.8% at the same 0% false-positive rate (separation AUC 0.857 -> 0.876).
# Every number in the census below was read under qwen3/600m at 0.89; the SHAPE of
# the argument (the classes interleave just below the cut) carries over, the cosines do not.
# INVARIANT: this is a KNOB, never a hard-coded constant, and it is corpus-dependent.
# Why: 0.89 was read off 51 labelled pairs from ONE project, ONE domain and one house
# style — every fact there opens "В лекции…", which inflates every score. On that set
# it caught 7 of the 8 duplicates a live run deposited with zero false positives, and
# the classes interleave just below it (a true duplicate at 0.870, genuinely distinct
# facts at 0.885 and 0.865), so lowering it does not catch "more thoroughly" — below
# ~0.86 cosine measures shared topic and vocabulary, not shared assertion.
#
# A full pair census of a live project (94 facts: 154 same-subject + 4217 cross-subject
# pairs) puts numbers on that interleaving and on what this knob can be worth:
#   - NO pair in the whole project reaches 0.89. The gate fires on the incoming fact
#     at apply time, not on what has settled, so a zero here is expected — but it also
#     means every near-duplicate that survives sits in 0.80–0.89.
#   - inside that band the classes do not separate: true duplicates at 0.808 / 0.840 /
#     0.856 / 0.872 / 0.881 interleave with genuinely distinct pairs at 0.828 / 0.834 /
#     0.836. The widest safe gap is thousandths, i.e. no threshold exists that catches
#     the band's duplicates without refusing its distinct facts.
# So this number is NOT the lever for the surviving duplicates, and tuning it down is
# how you buy false refusals. What does work is the agent's own comparison against the
# merge candidates served with the portion: on a re-run over consumed material it
# converted 11 of 13 comparable verdicts into `merge` while this gate fired zero times.
_section("tools", "Project memory")
MEMORY_DUPLICATE_FACT_THRESHOLD = setting(
    "MEMORY_DUPLICATE_FACT_THRESHOLD", float, default=0.82, min=0.0, max=1.0,
    label="Duplicate-fact cosine",
    help="Cosine at/above which a new fact is refused as restating a stored "
         "one (corpus-dependent knob — see config.py before touching).",
)


# ─── Agent sandbox (SYSTEM: agent-sandbox) ───────────────────────────────────
# The agent's console host. The backend knows only a host, a key and a path — never
# whether the far end is the dev compose service or the prod LXC (CT 703). That is
# what keeps the deployment difference out of the code. See docs/sandbox-access.md.
_section("tools", "Agent sandbox")
SANDBOX_SSH_HOST = setting(
    "SANDBOX_SSH_HOST", str, default="",
    label="Sandbox SSH host",
    help="Host of the agent console box. Empty host or key = the console "
         "tools are not served.",
)
SANDBOX_SSH_PORT = setting(
    "SANDBOX_SSH_PORT", int, default=22, min=1, max=65535,
    label="Sandbox SSH port",
    help="SSH port of the agent console box.",
)
SANDBOX_SSH_USER = setting(
    "SANDBOX_SSH_USER", str, default="agent",
    label="Sandbox SSH user",
    help="Unix account every console channel connects as (one shared "
         "account; separation is per-workspace).",
)
# INVARIANT(security): base64 of the PRIVATE key, and it lives only in env or in
# the operator's key file — never in the image or the repo.
# Why: both are rebuildable/readable by anyone with the source, which would make
# it a published credential. Base64 because a PEM's newlines do not survive .env
# parsing.
SANDBOX_SSH_KEY_B64 = setting(
    "SANDBOX_SSH_KEY_B64", "secret", default="",
    label="Sandbox SSH private key (base64)",
    help="Base64 of the PRIVATE key (a PEM's newlines do not survive .env "
         "parsing). Env wins over SANDBOX_SSH_KEY_FILE.",
)
# The bundled sandbox's keygen writes the pair into a named volume; the backend
# mounts it read-only and reads the private half from disk. Empty = no file leg
# (the external-sandbox contract: key in env only — the shape prod runs).
# WHY env-only, no admin row: it is a path inside the container that the compose
# file sets (docker-compose.public.yml), not an operator choice.
SANDBOX_SSH_KEY_FILE = os.environ.get("SANDBOX_SSH_KEY_FILE", "")


def _sandbox_key_from(env_value: str, file_path: str) -> str:
    """The key fold: env value, else base64 of the file, else empty.

    A missing file must resolve to EMPTY, not raise — an unset key means "the
    sandbox tool is not served" (SANDBOX_ENABLED below), the same valid-deploy
    stance as an unset host.
    """
    if env_value:
        return env_value
    if file_path and Path(file_path).is_file():
        return base64.b64encode(Path(file_path).read_bytes()).decode("ascii")
    return ""


# WHY the rebind (the instance_secret.resolve precedent behind SECRET_KEY
# above): every consumer reads the key at CALL time through settings — the
# agent-tool gate (agent/tools.py) and the SSH transport
# (routes/tool_api/sandbox/transport.py) — and settings._resolve ends in
# getattr(config, key), so folding the file leg INTO the key's own config
# value reaches all of them with no second read site to drift. Env always
# wins, so an external sandbox overrides the bundled key.
SANDBOX_SSH_KEY_B64 = _sandbox_key_from(SANDBOX_SSH_KEY_B64, SANDBOX_SSH_KEY_FILE)
# The sandbox is OPTIONAL: unset SANDBOX_SSH_HOST means the tool is not served at all
# (see agent_toolset). This is deliberately NOT a crash-on-missing-config case —
# a deploy without a sandbox is a valid deploy, and every other chat must still work.
SANDBOX_ENABLED = bool(SANDBOX_SSH_HOST and SANDBOX_SSH_KEY_B64)

# Wall-clock ceiling on a DETACHED run (`sandbox_bash(detach=true)`) — enforced by
# `timeout` on the sandbox, the same mechanism as the foreground path. Far above
# TURN_PROGRESS_GRACE_S and the 300 s foreground cap on purpose: the whole point of the
# mode is a run that outlives the turn (a test suite, a build). It still bounds the
# leak: the sandbox is shared by every user, and a client-side abandon does NOT kill
# the remote process, so unbounded detach would leak a process permanently.
SANDBOX_DETACH_TIMEOUT_S = setting(
    "SANDBOX_DETACH_TIMEOUT_S", int, default=14400, min=60,
    label="Detached run ceiling, s",
    help="Wall-clock bound on a detached sandbox_bash run (far above the "
         "turn budget on purpose — the mode exists for runs that outlive "
         "the turn).",
)
# GC threshold for finished detached-run directories (`.runs/{run_id}`). Reaped on
# the next detached spawn — a cleanup nobody schedules is a cleanup that never runs.
# A run dir older than this is finished by construction: SANDBOX_DETACH_TIMEOUT_S is
# a hard ceiling, so nothing spawned can still be alive after N days.
SANDBOX_RUNS_GC_DAYS = setting(
    "SANDBOX_RUNS_GC_DAYS", int, default=7, min=1,
    label="Run dirs GC, days",
    help="Finished detached-run directories older than this are reaped on "
         "the next spawn (the only scheduled GC the console has).",
)
# The model credential a FOREIGN agent CLI running in the sandbox authenticates with.
# Delivered per run over the SSH channel as LORE_EXTERNAL_AGENT_KEY, only for a call
# that asked for it (`sandbox_bash(with_external_agent_key=true)`) — never exported
# into the shared `agent` account's environment.
# INVARIANT(security): this value must never be interpolated into a command string.
# Why: the detached wrapper writes its command verbatim to `.runs/{run_id}/cmd`
# (routes/tool_api/sandbox._wrap_detached), and every run of every user reads the box
# as the same unix account — a secret placed in the command outlives the run on disk,
# which is strictly worse than the ambient environment this per-run delivery replaces.
SANDBOX_EXTERNAL_AGENT_KEY = setting(
    "SANDBOX_EXTERNAL_AGENT_KEY", "secret", default="",
    label="External agent CLI key",
    help="Model credential delivered per run to a foreign agent CLI that "
         "asked for it (sandbox_bash with_external_agent_key).",
)


# ─── ComfyUI image generation (SYSTEM: comfy-image-gen, agent-only tool: generate_image) ─
# ARCH: mirrors the SANDBOX_ENABLED posture — ComfyUI is an INTERNAL service an
# external MCP client cannot reach, so the generate_image tool is served ONLY via
# agent_toolset (the agent path) and NEVER added to AGENT_TOOLS. Unset
# COMFYUI_URL = the tool is simply not served (a valid deploy, not an error).
#
# ARCH: the refiner prompt, the workflow graph and the three sizes are ONE
# config per instance, edited here in admin; the shipped defaults are the
# configs/comfy_* files. The workflow is the operator's own ComfyUI "Export
# (API)" with the nodes Lore fills marked by `[lore:NAME]` in their title
# (see comfy_markers); it is validated on every write and at boot, and the
# generate_image launcher reads all of it live per call.
_section("tools", "ComfyUI image generation")
COMFYUI_URL = setting(
    "COMFYUI_URL", str, default="",
    label="ComfyUI URL",
    help="Internal ComfyUI service. Empty = the generate_image tool is not "
         "served.",
)
COMFYUI_TIMEOUT_S = setting(
    "COMFYUI_TIMEOUT_S", int, default=120, min=1,
    label="Generation poll deadline, s",
    help="Bound on the /prompt + /history poll loop of one generation.",
)
COMFYUI_ENABLED = bool(COMFYUI_URL)
_COMFY_CONFIGS = Path(__file__).parent / "configs"
COMFYUI_PROMPT = setting(
    "COMFYUI_PROMPT", "text",
    default=(_COMFY_CONFIGS / "comfy_prompt.txt").read_text(encoding="utf-8"),
    label="Prompt refinement instructions",
    help="Sent verbatim as the system message to the prompt refinement model, "
         "which must answer {\"prompt\": \"...\"}. On any failure the agent's "
         "raw description is used.",
)
COMFYUI_WORKFLOW = setting(
    "COMFYUI_WORKFLOW", "text",
    default=(_COMFY_CONFIGS / "comfy_workflow.json").read_text(encoding="utf-8"),
    validate=validate_workflow,
    label="Workflow (API format)",
    help="Paste ComfyUI \"Export (API)\". Mark the nodes Lore fills by adding "
         "to the node TITLE: [lore:prompt] (text, exactly one), [lore:seed] "
         "(seed / noise_seed, random per call; drop it to keep the workflow's "
         "seed), [lore:size] (width, height; without it the workflow's size "
         "applies), [lore:batch] (batch_size = image count; without it only one "
         "image per call). Every SaveImage node saves under lore/<run id>.",
)
COMFYUI_SIZE_SQUARE = setting(
    "COMFYUI_SIZE_SQUARE", str, default="1024x1024", validate=validate_size,
    label="Square size",
    help="WIDTHxHEIGHT filled into the [lore:size] node for orientation=square.",
)
COMFYUI_SIZE_PORTRAIT = setting(
    "COMFYUI_SIZE_PORTRAIT", str, default="832x1216", validate=validate_size,
    label="Portrait size",
    help="WIDTHxHEIGHT filled into the [lore:size] node for orientation=portrait.",
)
COMFYUI_SIZE_LANDSCAPE = setting(
    "COMFYUI_SIZE_LANDSCAPE", str, default="1216x832", validate=validate_size,
    label="Landscape size",
    help="WIDTHxHEIGHT filled into the [lore:size] node for orientation=landscape.",
)
# Prompt refinement: the refinement LLM model (falls back to CHAT_MODEL)
# and that call's HTTP timeout. The refiner reads NO chat history — see the
# no-history INVARIANT in routes/tool_api/image_gen.py.
#
# This is AUTHORITATIVE for refinement — the
# model selected in the chat is not an input (see the INVARIANT on
# _refine_prompt). WHY point it at a fast non-reasoning model: measured on the
# same real payload, `local/orange/reasoner` took 164.1s (4051 completion tokens,
# 17756 reasoning chars) for a 680-char answer that `local/orange/chat` produced
# in 4.4s (82 tokens, no reasoning). Rewriting a description into SD phrasing has
# nothing to reason about, and `reasoning_effort` is ignored by the router, so a
# reasoning model cannot be made cheap here — it just blows the turn budget.
COMFYUI_PROMPT_MODEL = setting(
    "COMFYUI_PROMPT_MODEL", str, fallback=("CHAT_MODEL",),
    label="Prompt refinement model",
    help="Fast non-reasoning model that expands the scene description into "
         "an SD prompt; empty = the default chat model.",
)
# 90, not the 30 copied from the ComfyUI
# per-request read (that endpoint only enqueues + answers at once; the long wait
# there is a separate poll deadline). 30s timed out every refinement and the
# seed-fallback was the only path ever taken. Kept at 90 after the refiner moved
# to a dedicated fast model (4.4s on the real payload): pure headroom now, but it
# still covers a slower model someone points COMFYUI_PROMPT_MODEL at.
# Budget: TURN_PROGRESS_GRACE_S=300 bounds the turn and the ComfyUI poll deadline is
# COMFYUI_TIMEOUT_S=120, so 90+120=210 leaves ~90s for the agent's own work.
# Not 120: that puts the sum at the edge of the budget.
COMFYUI_PROMPT_TIMEOUT_S = setting(
    "COMFYUI_PROMPT_TIMEOUT_S", int, default=90, min=1,
    label="Refinement LLM timeout, s",
    help="HTTP timeout of the refinement call; on failure the raw prompt is "
         "used.",
)
# Bounds how many generate_image jobs run at
# once on the arq worker. A burst of tool calls queues in arq instead of
# hammering one ComfyUI box. Default 2 — one generation is ~10–20s of GPU, so 2
# keeps a second request responsive without over-scheduling a single box.
# Shown with the rest of ComfyUI in the tools tab, where an operator looks for
# it. max(1, …) keeps the env-value clamp the declaration's min bound does not
# apply to raw env reads.
_section("tools", "ComfyUI image generation")
COMFY_CONCURRENCY = max(1, setting(
    "COMFY_CONCURRENCY", int, default=2, min=1, effect="restart",
    label="ComfyUI concurrency",
    help="Concurrent generate_image jobs on the worker. Set in .env — the "
         "semaphore is built once per worker.",
))


def count_tokens_approx(text: str) -> int:
    return int(len(text) / 4 * RETRIEVAL_TOKEN_SAFETY_MARGIN)
