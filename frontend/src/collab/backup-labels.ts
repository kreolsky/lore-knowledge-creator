/**
 * Backup-label constants — frontend mirror of the backend `auto_backup.py` registry.
 *
 * ARCH: single module for the label string values the frontend cares about + the
 * toast allowlist map. This is a HAND-MAINTAINED MIRROR of the backend contract
 * (backend/auto_backup.py), NOT auto-synced — when adding a new label on the backend,
 * touch this module too so the two sides stay consistent.
 */

import type { TranslationKey } from '../i18n/en';

/** Session-start safety backup on document open (content-hash dedup). */
export const LABEL_SAFETY_OPEN = 'safety-open';
/** Auto-backup before a new editor's first edit (editor handoff). */
export const LABEL_EDITOR_HANDOFF = 'editor-handoff';
/** Auto-backup on content loss (large delete/replace). */
export const LABEL_AUTO_BACKUP = 'auto-backup';
/** Per-user "last session" snapshot — one upserted row per (doc, editor). */
export const LABEL_LAST_SESSION = 'last-session';
/** before-restore safety snapshot (exactly one per doc, refreshed on restore). */
export const LABEL_BEFORE_RESTORE = '_backup';

// INVARIANT: toast only on AUTOMATIC backups (content-loss + editor-handoff +
// safety-open) — never manual checkpoints, '_backup'/'last-session', or agent-auto.
// Why: manual labels are arbitrary (explicit allowlist by label); agent-auto fires
// 4–8× per agent turn — a toast per splice would spam.
export const AUTO_BACKUP_TOAST_KEYS: Record<string, TranslationKey> = {
  [LABEL_AUTO_BACKUP]: 'autoBackupCreated',
  [LABEL_EDITOR_HANDOFF]: 'handoffBackupCreated',
  [LABEL_SAFETY_OPEN]: 'safetyBackupCreated',
};
