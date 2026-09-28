/** Admin: one registry tab of instance settings — DB overrides over env over
 * defaults (SYSTEM: instance-settings).
 *
 * Renders ONLY the tab's keys, grouped under a heading per registry section
 * (the second segment of the group path; the tab itself is the header crumb);
 * one key = one cabinet field
 * block: label + source chip (`| default` / `| .env` / `| override`) + help +
 * control + explicit Save (+ Reset when overridden) + inline result line.
 * A `choices` key picks its value from a Dropdown instead of typing it; a
 * `visible_if` row (e.g. one provider's key) shows only while the key it
 * names holds that value — the SAVED value, so it switches on Save. A `text`
 * key (a pasted workflow, a prompt) is a full-width monospace textarea with
 * its buttons wrapping below it.
 * Restart keys are read-only (disabled input + the .env note) — their readers
 * are boot-frozen, so an override row could never take effect.
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, CenterHeading, Dropdown, FieldCheckbox, FieldInput, FieldTextarea } from '../ui';
import {
  listAdminSettings, putAdminSetting, resetAdminSetting,
  type AdminSettingEntry, type SettingsTabId,
} from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import type { TranslationKey } from '../../i18n/en';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';

/** The chip word for an entry's source — the RoleChip idiom (text, no badge). */
function sourceLabel(source: AdminSettingEntry['source'], t: (k: TranslationKey) => string): string {
  if (source === 'override') return t('sourceOverride');
  if (source === '.env') return t('sourceEnv');
  return t('sourceDefault');
}

export function SettingsSection({ tab }: { tab: SettingsTabId }) {
  const { t } = useTranslation();
  const [entries, setEntries] = useState<AdminSettingEntry[] | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  // Bumped after every full refetch: fields remount (key includes it), so a
  // Reset adopts the fresh env/default value instead of keeping the override
  // draft the operator just discarded.
  const [reloadSeq, setReloadSeq] = useState(0);
  // t rides a ref: reload is a stable useCallback consumed by the mount
  // effect (t in its deps would refetch the list on every language switch).
  const tRef = useRef(t);
  tRef.current = t;

  const reload = useCallback(async () => {
    try {
      setEntries(await listAdminSettings());
      setReloadSeq(n => n + 1);
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      useAppStore.getState().showToast(tRef.current('adminListLoadFailed'), 'error');
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const handleSaved = useCallback((updated: AdminSettingEntry) => {
    setEntries(prev => (prev ? prev.map(e => (e.key === updated.key ? updated : e)) : prev));
  }, []);

  // Registry order preserved; ALL same-section entries merge into one group
  // (first appearance wins the position). INVARIANT: one group per section
  // name. Why: the group is keyed by its section, and config.py may aim two
  // non-adjacent declaration blocks at the same section — a second group
  // with the same key made React duplicate/pin the heading across tab switches.
  const groups = useMemo(() => {
    const valueOf = new Map((entries ?? []).map(e => [e.key, e.value]));
    const bySection = new Map<string, AdminSettingEntry[]>();
    for (const e of entries ?? []) {
      if (e.tab !== tab) continue;
      if (e.visible_if && valueOf.get(e.visible_if.key) !== e.visible_if.value) continue;
      const list = bySection.get(e.section);
      if (list) list.push(e);
      else bySection.set(e.section, [e]);
    }
    return Array.from(bySection, ([section, list]) => ({ section, entries: list }));
  }, [entries, tab]);

  return (
    <>
      {loadFailed && <p className="text-xs text-red mb-2">{t('adminListLoadFailed')}</p>}
      <div className="px-1.5">
        {groups.map((grp, i) => (
          <Fragment key={grp.section}>
            {/* Same heading as the Users section's "Add user" — one style for
                every sub-section of the admin center. */}
            <div data-section-title={grp.section} className={i === 0 ? '' : 'mt-8'}>
              <CenterHeading>{grp.section}</CenterHeading>
            </div>
            {grp.entries.map(entry => (
              <SettingField
                key={`${entry.key}#${reloadSeq}`}
                entry={entry}
                onSaved={handleSaved}
                onReset={reload}
              />
            ))}
          </Fragment>
        ))}
      </div>
    </>
  );
}

function SettingField({ entry, onSaved, onReset }: {
  entry: AdminSettingEntry;
  onSaved: (updated: AdminSettingEntry) => void;
  /** Full-list refetch after a Reset (the DELETE response carries no value). */
  onReset: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState(String(entry.value));
  const [checked, setChecked] = useState(Boolean(entry.value));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const numeric = Number(text);
  const parseOk = entry.type === 'int'
    ? /^-?\d+$/.test(text.trim()) && Number.isSafeInteger(numeric)
    : entry.type === 'float'
      ? text.trim() !== '' && Number.isFinite(numeric)
      : true;
  const nextValue: string | number | boolean = entry.type === 'bool'
    ? checked
    : entry.type === 'int' || entry.type === 'float'
      ? numeric
      : text;
  const unchanged = entry.type === 'bool'
    ? checked === Boolean(entry.value)
    : text === String(entry.value);
  const readOnly = entry.effect === 'restart';

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const updated = await putAdminSetting(entry.key, nextValue);
      onSaved(updated);
      setText(String(updated.value));
      setChecked(Boolean(updated.value));
      setMsg({ ok: true, text: t('saved') });
    } catch (err: unknown) {
      setMsg({ ok: false, text: serverRefusalDetail(err) ?? t('settingsSaveFailed') });
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await resetAdminSetting(entry.key);
      await onReset();
    } catch (err: unknown) {
      setMsg({ ok: false, text: serverRefusalDetail(err) ?? t('settingsSaveFailed') });
      setBusy(false);
    }
  };

  const control = readOnly ? (
    <FieldInput className="flex-1" value={String(entry.value)} disabled />
  ) : entry.choices ? (
    <Dropdown
      value={text}
      options={entry.choices.map(c => ({ value: c, label: c }))}
      onSelect={setText}
      size="lg"
      placement="bottom"
      disabled={busy}
    />
  ) : entry.type === 'bool' ? (
    <FieldCheckbox checked={checked} onChange={setChecked} label={t('enabled')} disabled={busy} />
  ) : entry.type === 'text' ? (
    <FieldTextarea
      className="w-full"
      mono
      rows={12}
      value={text}
      onChange={e => setText(e.target.value)}
      disabled={busy}
    />
  ) : (
    <FieldInput
      className={entry.type === 'int' || entry.type === 'float' ? 'w-[160px]' : 'flex-1'}
      type={entry.type === 'secret' ? 'password' : undefined}
      value={text}
      onChange={e => setText(e.target.value)}
      disabled={busy}
      autoComplete="off"
    />
  );

  return (
    <div data-setting-key={entry.key} className="py-5 border-b border-border-soft">
      <label className="block text-xs text-text-dim mb-1.5">
        {entry.label}
        <span className="whitespace-nowrap"> | {sourceLabel(entry.source, t)}</span>
      </label>
      {entry.help && <p className="text-xs text-text-dim mb-2">{entry.help}</p>}
      <div className={`flex gap-2 items-center${entry.type === 'text' ? ' flex-wrap' : ''}`}>
        {control}
        {!readOnly && (
          <Button variant="primary" size="lg" disabled={busy || unchanged || !parseOk} onClick={save}>
            {t('save')}
          </Button>
        )}
        {!readOnly && entry.source === 'override' && (
          <Button size="lg" disabled={busy} onClick={reset}>
            {t('settingsReset')}
          </Button>
        )}
      </div>
      {msg && <p className={`text-xs mt-1 ${msg.ok ? 'text-green' : 'text-red'}`}>{msg.text}</p>}
      {readOnly && (
        <p className="text-xs mt-1 text-text-dim">{t('settingsRestartNote', { env: entry.env })}</p>
      )}
    </div>
  );
}
