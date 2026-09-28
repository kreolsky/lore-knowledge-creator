import { describe, it, expect } from 'vitest';
import {
  LABEL_SAFETY_OPEN,
  LABEL_EDITOR_HANDOFF,
  LABEL_AUTO_BACKUP,
  LABEL_LAST_SESSION,
  LABEL_BEFORE_RESTORE,
  AUTO_BACKUP_TOAST_KEYS,
} from '../backup-labels';

describe('backup-labels constants', () => {
  it('exports the wire values for each label', () => {
    expect(LABEL_SAFETY_OPEN).toBe('safety-open');
    expect(LABEL_EDITOR_HANDOFF).toBe('editor-handoff');
    expect(LABEL_AUTO_BACKUP).toBe('auto-backup');
    expect(LABEL_LAST_SESSION).toBe('last-session');
    expect(LABEL_BEFORE_RESTORE).toBe('_backup');
  });

  it('toast map has exactly the 3 automatic-backup keys', () => {
    expect(Object.keys(AUTO_BACKUP_TOAST_KEYS).sort()).toEqual(
      ['auto-backup', 'editor-handoff', 'safety-open'],
    );
  });

  it('does NOT toast on last-session or _backup restore labels', () => {
    expect(AUTO_BACKUP_TOAST_KEYS[LABEL_LAST_SESSION]).toBeUndefined();
    expect(AUTO_BACKUP_TOAST_KEYS[LABEL_BEFORE_RESTORE]).toBeUndefined();
  });
});
