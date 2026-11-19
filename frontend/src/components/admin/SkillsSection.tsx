/** Admin: instance skills — the missing middle layer of the served overlay
 * (project > instance > shipped; SYSTEM: instance-settings).
 *
 * One card per DISTINCT union entry, shaped like the projects/users cards:
 * chevron · name · "modified" mark · reset-or-delete · enabled checkbox.
 * The checkbox acts IMMEDIATELY: unchecking a shipped skill PUTs a tombstone
 * ({enabled:false}, contentless), re-checking a tombstone DELETEs the row
 * (the shipped file reappears), toggling an instance row PUTs its flag.
 * Expanding a card opens the editor over the skill's CURRENT body (instance
 * content, else the shipped body as the override draft); Save PUTs it to the
 * row's name, or — for a new skill — to the name extracted from the
 * frontmatter; the server 422s a mismatch and the detail renders inline.
 * Reset (armed) DELETEs an overridden row so the shipped body serves again;
 * delete (armed) removes a skill that has no shipped fallback.
 * Upload zip POSTs a bundle (SKILL.md + scripts/ + references/); skipped
 * entries and refusals surface as toasts; each card lists its bundle files,
 * and clicking a path toggles a read-only view of its text (fetched on click).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, RotateCcw, Trash2, Upload } from 'lucide-react';
import { Button, CenterHeading, FieldCheckbox, FieldTextarea, IconButton } from '../ui';
import {
  deleteAdminSkill, importSkillArchive, listAdminSkills, readAdminSkillFile, putAdminSkill, type AdminSkillEntry,
} from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { useArmedAction } from '../../hooks/useArmedAction';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';

/** Sentinel editing scope for the new-skill editor (row scopes are names). */
const NEW_SKILL = '__new__';

/** The `name:` value of the body's first `---` block — mirrors the server's
 * frontmatter_name (agent_skills.py) so a new skill PUTs to the name the
 * catalog will overlay by; the server re-validates and 422s a miss. */
function frontmatterName(content: string): string | null {
  const normalized = content.replace(/\r\n/g, '\n');
  if (!normalized.startsWith('---\n')) return null;
  const end = normalized.indexOf('\n---', 4);
  if (end < 0) return null;
  const m = normalized.slice(4, end).match(/^name:\s*(\S+)\s*$/m);
  return m?.[1] ?? null;
}

/** Armed (two-click) icon action: reset to shipped for an overridden row,
 * delete for a skill with no shipped fallback — same DELETE, different intent. */
function SkillArmedButton({ kind, onFire, disabled }: { kind: 'reset' | 'delete'; onFire: () => void; disabled?: boolean }) {
  const { t } = useTranslation();
  const action = useArmedAction();
  return (
    <IconButton
      size="sm"
      danger
      filled={action.armed}
      onClick={() => action.handleClick(onFire)}
      onMouseLeave={action.disarm}
      disabled={disabled}
      title={kind === 'reset' ? t('skillReset') : t('delete')}
    >
      {kind === 'reset' ? <RotateCcw size={14} /> : <Trash2 size={14} />}
    </IconButton>
  );
}

/** Draft-holding editor; the parent resolves the target name and fires the PUT. */
function SkillEditor({ initial, disabled, onSave, onCancel }: {
  initial: string;
  disabled?: boolean;
  onSave: (content: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(initial);
  return (
    <div className="mt-2 flex flex-col gap-2">
      <FieldTextarea
        onPanel
        value={draft}
        onChange={e => setDraft(e.target.value)}
        rows={12}
        disabled={disabled}
        autoFocus
      />
      <div className="flex gap-2 items-center">
        <Button variant="primary" disabled={disabled || !draft.trim()} onClick={() => onSave(draft)}>
          {t('save')}
        </Button>
        <Button disabled={disabled} onClick={onCancel}>{t('cancel')}</Button>
      </div>
    </div>
  );
}

/** A bundle path; click toggles its text, fetched once per open. */
function SkillFileRow({ skillName, path }: { skillName: string; path: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState<string | null>(null);
  const toggle = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    setContent(null);
    try {
      setContent(await readAdminSkillFile(skillName, path));
    } catch (err: unknown) {
      setOpen(false);
      useAppStore.getState().showToast(serverRefusalDetail(err) ?? t('skillFileLoadFailed'), 'error');
    }
  };
  return (
    <li>
      <Button variant="ghost" size="sm" onClick={() => void toggle()}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {path}
      </Button>
      {open && (
        <pre className="mt-1 mb-2 p-2 font-mono bg-surface border border-border-soft text-text overflow-x-auto max-h-96 whitespace-pre">
          {content ?? t('loading')}
        </pre>
      )}
    </li>
  );
}

export function SkillsSection() {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<AdminSkillEntry[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ scope: string; text: string } | null>(null);
  const [importing, setImporting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const tRef = useRef(t);
  tRef.current = t;

  const reload = useCallback(async () => {
    try {
      setSkills(await listAdminSkills());
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      useAppStore.getState().showToast(tRef.current('adminListLoadFailed'), 'error');
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const replaceRow = (updated: AdminSkillEntry) => {
    setSkills(prev => (prev ? prev.map(s => (s.name === updated.name ? updated : s)) : prev));
  };

  /** Save content: `name === null` is the new-skill flow — the name comes out
   * of the frontmatter (client-extracted, server-validated). */
  const saveContent = async (name: string | null, content: string) => {
    const scope = name ?? NEW_SKILL;
    const target = name ?? frontmatterName(content);
    if (!target) {
      setMsg({ scope, text: t('skillNoFrontmatterName') });
      return;
    }
    setBusy(scope);
    setMsg(null);
    try {
      const updated = await putAdminSkill(target, { content });
      if (name) {
        replaceRow(updated);
      } else {
        // A new name joins the union — refetch for the server's sort order.
        await reload();
      }
      setEditing(null);
    } catch (err: unknown) {
      setMsg({ scope, text: serverRefusalDetail(err) ?? t('skillSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const toggleEnabled = async (skill: AdminSkillEntry) => {
    setBusy(skill.name);
    setMsg(null);
    try {
      if (skill.source === 'shipped') {
        // No instance row yet: unchecking creates the tombstone.
        replaceRow(await putAdminSkill(skill.name, { enabled: false }));
      } else if (skill.content === null) {
        // A pure tombstone: re-enabling means removing the row — the shipped
        // file reappears (an enabled contentless row would serve nothing).
        await deleteAdminSkill(skill.name);
        await reload();
      } else {
        replaceRow(await putAdminSkill(skill.name, { enabled: !skill.enabled }));
      }
    } catch (err: unknown) {
      setMsg({ scope: skill.name, text: serverRefusalDetail(err) ?? t('skillSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const removeSkill = async (skill: AdminSkillEntry) => {
    setBusy(skill.name);
    setMsg(null);
    try {
      await deleteAdminSkill(skill.name);
      await reload();
    } catch (err: unknown) {
      setMsg({ scope: skill.name, text: serverRefusalDetail(err) ?? t('skillDeleteFailed') });
    } finally {
      setBusy(null);
    }
  };

  const importArchive = async (file: File) => {
    const { showToast } = useAppStore.getState();
    setImporting(true);
    try {
      const imported = await importSkillArchive(file);
      await reload();
      if (imported.skipped.length > 0) {
        showToast(t('skillImportSkipped', { name: imported.name, files: imported.skipped.join(', ') }), 'warning');
      } else {
        showToast(t('skillImported', { name: imported.name }), 'info');
      }
    } catch (err: unknown) {
      showToast(t('skillImportFailed', { detail: serverRefusalDetail(err) ?? t('skillSaveFailed') }), 'error');
    } finally {
      setImporting(false);
    }
  };

  const closeEditor = () => {
    setEditing(null);
    setMsg(null);
  };

  return (
    <>
      {/* Variant of the admin heading: mb-1 — the hint hugs the rule. */}
      <CenterHeading className="text-ui-md font-semibold text-text pb-1.5 border-b border-border mb-1">{t('skills')}</CenterHeading>
      <p className="text-xs text-text-dim mb-3">{t('skillsHint')}</p>
      {loadFailed && <p className="text-xs text-red mb-2">{t('adminListLoadFailed')}</p>}
      {editing === NEW_SKILL ? (
        <div className="py-4 border-b border-border-soft">
          <SkillEditor initial="" disabled={busy === NEW_SKILL} onSave={c => void saveContent(null, c)} onCancel={closeEditor} />
          {msg?.scope === NEW_SKILL && <p className="text-xs mt-1 text-red">{msg.text}</p>}
        </div>
      ) : (
        <div className="flex gap-2">
          <Button variant="primary" onClick={() => { setEditing(NEW_SKILL); setMsg(null); }}>
            {t('skillNew')}
          </Button>
          <Button disabled={importing} onClick={() => fileInputRef.current?.click()}>
            <Upload size={14} /> {t('skillImportZip')}
          </Button>
        </div>
      )}
      <input
        ref={fileInputRef}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        onChange={e => {
          const file = e.target.files?.[0];
          e.target.value = '';
          if (file) void importArchive(file);
        }}
      />
      <div className="flex flex-col gap-0.5 mt-3">
      {(skills ?? []).map(skill => {
        // Content over a shipped file — reset brings the shipped body back;
        // content with no shipped fallback — only delete applies.
        const overridden = skill.content !== null && skill.shipped;
        const expanded = editing === skill.name;
        const rowMsg = msg?.scope === skill.name ? msg.text : null;
        return (
          <section
            key={skill.name}
            data-skill-name={skill.name}
            className={`hover:bg-surface2 transition-colors duration-150 ${expanded ? 'bg-surface2' : 'bg-surface'}`}
          >
            <div className="py-2.5 px-3.5 flex items-center gap-3">
              <IconButton size="sm" onClick={() => (expanded ? closeEditor() : (setEditing(skill.name), setMsg(null)))}>
                {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </IconButton>
              <span className={`text-sm font-medium truncate ${skill.enabled ? 'text-text' : 'text-text-dim'}`}>
                {skill.name}
              </span>
              {overridden && (
                <span className="text-xs text-text-dim whitespace-nowrap">{t('skillModified')}</span>
              )}
              <span className="flex-1" />
              {overridden && (
                <SkillArmedButton kind="reset" disabled={busy === skill.name} onFire={() => void removeSkill(skill)} />
              )}
              {skill.content !== null && !skill.shipped && (
                <SkillArmedButton kind="delete" disabled={busy === skill.name} onFire={() => void removeSkill(skill)} />
              )}
              <FieldCheckbox
                checked={skill.enabled}
                onChange={() => void toggleEnabled(skill)}
                label=""
                title={t('enabled')}
                disabled={busy === skill.name}
              />
            </div>
            {skill.files.length > 0 && (
              <ul className="px-3.5 pb-2 pl-12 text-xs text-text-dim">
                {skill.files.map(path => <SkillFileRow key={path} skillName={skill.name} path={path} />)}
              </ul>
            )}
            {expanded && (
              <div className="px-3.5 pb-3">
                <SkillEditor
                  initial={skill.content ?? skill.shipped_body ?? ''}
                  disabled={busy === skill.name}
                  onSave={c => void saveContent(skill.name, c)}
                  onCancel={closeEditor}
                />
              </div>
            )}
            {rowMsg && <p className="text-xs px-3.5 pb-2 text-red">{rowMsg}</p>}
          </section>
        );
      })}
      </div>
    </>
  );
}
