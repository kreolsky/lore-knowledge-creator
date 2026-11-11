/** Cabinet section: the user's own API keys for other services (SYSTEM: user-secrets).
 *
 * Same row shape as the MCP sections: one collapsible row per key (chevron ·
 * name · hosts · enabled · armed delete), the form in the expanded panel, a
 * create panel above the list. The auth shape is picked from client presets
 * that map onto the three stored fields (header_name, header_template,
 * query_param); a row's preset is derived from those fields, never stored.
 * The value field is write-only.
 */

import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import { cabinetFetch } from '../../api/cabinet';
import { createArmedAction, type ArmedActionCore } from '../../hooks/useArmedAction';
import { Button, CenterHeading, FieldCheckbox, FieldInput, IconButton, SegmentedControl } from '../ui';
import { useTranslation } from '../../i18n';

/** One personal key row from GET /api/cabinet/secrets — metadata only.
 * INVARIANT(security): the value never reaches the client; `is_set` false
 * means "re-enter key" (the ciphertext no longer decrypts).
 * Why: the browser is outside the vault's trust boundary — chat frames relay
 * payloads toward the model and a leaked session cookie reads the cabinet —
 * so the value exists only in the server-side Fernet ciphertext. */
interface SecretRow {
  name: string;
  description: string;
  hosts: string[];
  header_name: string;
  header_template: string;
  query_param: string;
  enabled: boolean;
  is_set: boolean;
  updated_at: string | null;
}

type Preset = 'bearer' | 'xapikey' | 'basic' | 'query' | 'custom';

// Server defaults (user_secrets.DEFAULT_*) — also what Query mode leaves in
// the header fields, since the server sends no header when query_param is set.
const DEFAULT_HEADER = 'Authorization';
const DEFAULT_TEMPLATE = 'Bearer {{value}}';

/** Header presets: (header_name, header_template); Query and Custom are not here. */
const HEADER_PRESETS: Record<'bearer' | 'xapikey' | 'basic', [string, string]> = {
  bearer: [DEFAULT_HEADER, DEFAULT_TEMPLATE],
  xapikey: ['X-API-Key', '{{value}}'],
  basic: [DEFAULT_HEADER, 'Basic {{value_b64}}'],
};

/** A row's preset, derived from its stored fields; no match ⇒ Custom. */
function derivePreset(row: SecretRow): Preset {
  if (row.query_param) return 'query';
  const hit = (Object.keys(HEADER_PRESETS) as (keyof typeof HEADER_PRESETS)[]).find(p => {
    const [name, template] = HEADER_PRESETS[p];
    return row.header_name.toLowerCase() === name.toLowerCase() && row.header_template === template;
  });
  return hit ?? 'custom';
}

/** Split the hosts input (comma/semicolon/space separated) into entries;
 * the backend re-validates and canonicalizes each `host[:port]` / `*.domain`. */
function parseHosts(input: string): string[] {
  return input.split(/[,;\s]+/).map(h => h.trim()).filter(Boolean);
}

type SecretBody = Record<string, unknown>;

// WHY: `+` is outside the secret-name charset, so no row can collide with the create panel.
const NEW_PANEL = '+new';

/** Create/edit form — one instance at a time; `key` at the mount site resets it.
 * INVARIANT(security): the value field is write-only — the API never returns
 * it, and the form never echoes it back (edit prefill leaves it empty).
 * Why: an echoed value would land in React state, devtools and any future
 * toast — the exact surfaces the vault keeps the key out of. */
function SecretForm({ initial, disabled, error, onSave, onCancel }: {
  initial: SecretRow | null;
  disabled: boolean;
  error: string | null;
  onSave: (body: SecretBody) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const initialPreset = initial ? derivePreset(initial) : 'bearer';
  const [name, setName] = useState(initial?.name ?? '');
  const [description, setDescription] = useState(initial?.description ?? '');
  const [hostsInput, setHostsInput] = useState(initial?.hosts.join(', ') ?? '');
  const [value, setValue] = useState('');
  const [preset, setPreset] = useState<Preset>(initialPreset);
  const [queryParam, setQueryParam] = useState(initial?.query_param || 'key');
  const [headerName, setHeaderName] = useState(initialPreset === 'custom' ? initial?.header_name ?? '' : '');
  const [headerTemplate, setHeaderTemplate] = useState(initialPreset === 'custom' ? initial?.header_template ?? '' : '');
  // A non-default auth must not hide behind a closed toggle.
  const [advancedOpen, setAdvancedOpen] = useState(initialPreset !== 'bearer');

  const hosts = parseHosts(hostsInput);
  // Moving a key needs the value again — the server refuses otherwise (see
  // _secret_update_sets); this only says so before the click.
  const hostsChanged = !!initial && [...hosts].sort().join() !== [...initial.hosts].sort().join();
  const valuePlaceholder = [
    hostsChanged && t('secretHostsNeedValue'),
    initial && (initial.is_set ? t('secretKeepValue') : t('secretReenter')),
    preset === 'basic' && t('secretBasicValuePlaceholder'),
    t('secretValuePlaceholder'),
  ].find(Boolean) as string;

  const authFields = (): SecretBody => {
    if (preset === 'query') {
      return { header_name: DEFAULT_HEADER, header_template: DEFAULT_TEMPLATE, query_param: queryParam.trim() };
    }
    const [h, tpl] = preset === 'custom'
      ? [headerName.trim() || DEFAULT_HEADER, headerTemplate.trim() || DEFAULT_TEMPLATE]
      : HEADER_PRESETS[preset];
    // query_param goes on every save: '' clears a former Query row.
    return { header_name: h, header_template: tpl, query_param: '' };
  };

  const save = () => {
    onSave({
      ...(initial ? {} : { name: name.trim() }),
      description: description.trim(),
      hosts,
      ...authFields(),
      ...(value ? { value } : {}),
    });
  };

  const canSave = hosts.length > 0
    && (preset !== 'query' || !!queryParam.trim())
    && (initial ? !(hostsChanged && !value) : !!name.trim() && !!value);

  return (
    <div className="flex flex-col gap-2">
      {initial && !initial.is_set && (
        <p className="text-xs text-red">{t('secretReenter')} — {t('secretReenterHint')}</p>
      )}
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)] gap-2">
        {/* The server never renames — PATCH addresses the row by name. */}
        <FieldInput
          onPanel
          className="font-mono"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder={t('secretNamePlaceholder')}
          maxLength={64}
          disabled={disabled || !!initial}
          autoFocus={!initial}
        />
        <FieldInput
          onPanel
          value={description}
          onChange={e => setDescription(e.target.value)}
          placeholder={t('secretDescriptionPlaceholder')}
          maxLength={2000}
          disabled={disabled}
        />
      </div>
      <FieldInput
        onPanel
        className="font-mono"
        value={hostsInput}
        onChange={e => setHostsInput(e.target.value)}
        placeholder={t('secretHostsPlaceholder')}
        disabled={disabled}
      />
      <p className="text-xs text-text-dim -mt-1">{t('secretHostsHint')}</p>
      <FieldInput
        onPanel
        type="password"
        value={value}
        onChange={e => setValue(e.target.value)}
        placeholder={valuePlaceholder}
        autoComplete="new-password"
        maxLength={8192}
        disabled={disabled}
      />
      {/* The key field names its destination, at the exact field where the
          value is typed. */}
      {hosts.length > 0 && (
        <p className="text-xs text-text-dim font-mono -mt-1">
          {t('secretGoesTo', { hosts: hosts.join(' · ') })}
        </p>
      )}
      <div>
        <Button onClick={() => setAdvancedOpen(open => !open)} disabled={disabled}>
          {advancedOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          {t('secretAdvanced')}
        </Button>
      </div>
      {advancedOpen && (
        <>
          <p className="text-xs text-text-dim">{t('secretAuthHint')}</p>
          <div>
            <SegmentedControl
              ariaLabel={t('secretAuthHint')}
              options={[
                { value: 'bearer', label: 'Bearer' },
                { value: 'xapikey', label: 'X-API-Key' },
                { value: 'basic', label: 'Basic' },
                { value: 'query', label: t('secretPresetQuery') },
                { value: 'custom', label: t('secretPresetCustom') },
              ]}
              value={preset}
              onChange={p => setPreset(p as Preset)}
              disabled={disabled}
            />
          </div>
          {preset === 'query' && (
            <FieldInput
              onPanel
              className="font-mono"
              value={queryParam}
              onChange={e => setQueryParam(e.target.value)}
              placeholder={t('secretQueryParamPlaceholder')}
              maxLength={64}
              disabled={disabled}
            />
          )}
          {preset === 'custom' && (
            <>
              <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-2">
                <FieldInput
                  onPanel
                  className="font-mono"
                  value={headerName}
                  onChange={e => setHeaderName(e.target.value)}
                  placeholder={t('secretHeaderNamePlaceholder')}
                  disabled={disabled}
                />
                <FieldInput
                  onPanel
                  className="font-mono"
                  value={headerTemplate}
                  onChange={e => setHeaderTemplate(e.target.value)}
                  placeholder={t('secretHeaderTemplatePlaceholder')}
                  disabled={disabled}
                />
              </div>
              <p className="text-xs text-text-dim -mt-1">{t('secretHeaderHint')}</p>
            </>
          )}
        </>
      )}
      <div className="flex gap-2 items-center">
        <Button variant="primary" onClick={save} disabled={disabled || !canSave}>
          {t('save')}
        </Button>
        <Button onClick={onCancel} disabled={disabled}>{t('cancel')}</Button>
        {error && <span className="text-xs text-red">{error}</span>}
      </div>
    </div>
  );
}

export function PersonalKeysSection() {
  const { t } = useTranslation();
  const [secrets, setSecrets] = useState<SecretRow[] | null>(null);
  const [loadError, setLoadError] = useState('');
  // NEW_PANEL = the create panel; otherwise the expanded row's name.
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ id: string; text: string } | null>(null);
  const [deletedFlash, setDeletedFlash] = useState(false);
  // INVARIANT(security): delete-arming is keyed by the secret's NAME — one
  // armed row at a time. Why: a shared boolean flag (the single-button hook)
  // lights every row's delete at once, and since the sole key 'on' matches any
  // row's handleClick, a second click on a DIFFERENT row would delete that row
  // without its own confirmation. The keyed core keeps arming and execution on
  // the same row (same shape as the table-block toolbar, SYSTEM: armed-action).
  const [armedDelete, setArmedDelete] = useState<string | null>(null);
  const deleteArmedRef = useRef<ArmedActionCore<string> | null>(null);
  if (deleteArmedRef.current === null) {
    deleteArmedRef.current = createArmedAction<string>(2000, setArmedDelete);
  }
  const deleteArmed = deleteArmedRef.current;
  useEffect(() => () => deleteArmed.dispose(), [deleteArmed]);

  useEffect(() => {
    let alive = true;
    cabinetFetch('secrets', null, 'GET').then(([data, err]) => {
      if (!alive) return;
      if (err) { setLoadError(err); return; }
      setSecrets((data?.secrets ?? []) as SecretRow[]);
    });
    return () => { alive = false; };
  }, []);

  const replaceRow = (name: string, row: SecretRow) =>
    setSecrets(prev => (prev ?? []).map(s => (s.name === name ? row : s)));

  const closeForm = () => { setExpanded(null); setMsg(null); };

  const create = async (body: SecretBody) => {
    setBusy(NEW_PANEL);
    setMsg(null);
    const [data, err] = await cabinetFetch('secrets', body, 'POST');
    setBusy(null);
    if (err) { setMsg({ id: NEW_PANEL, text: err }); return; }
    setSecrets(prev => [...(prev ?? []), data as SecretRow].sort((a, b) => a.name.localeCompare(b.name)));
    setExpanded(null);
  };

  const save = async (row: SecretRow, body: SecretBody) => {
    setBusy(row.name);
    setMsg(null);
    const [data, err] = await cabinetFetch(`secrets/${encodeURIComponent(row.name)}`, body);
    setBusy(null);
    if (err) { setMsg({ id: row.name, text: err }); return; }
    replaceRow(row.name, data as SecretRow);
    setExpanded(null);
  };

  const toggleEnabled = async (row: SecretRow, enabled: boolean) => {
    setBusy(row.name);
    setMsg(null);
    const [data, err] = await cabinetFetch(`secrets/${encodeURIComponent(row.name)}`, { enabled });
    setBusy(null);
    if (err) { setMsg({ id: row.name, text: err }); return; }
    replaceRow(row.name, data as SecretRow);
  };

  const remove = async (row: SecretRow) => {
    setBusy(row.name);
    setMsg(null);
    const [, err] = await cabinetFetch(`secrets/${encodeURIComponent(row.name)}`, null, 'DELETE');
    setBusy(null);
    if (err) { setMsg({ id: row.name, text: err }); return; }
    setSecrets(prev => (prev ?? []).filter(s => s.name !== row.name));
    if (expanded === row.name) setExpanded(null);
    setDeletedFlash(true);
    setTimeout(() => setDeletedFlash(false), 2000);
  };

  return (
    <div>
      <CenterHeading>{t('personalKeys')}</CenterHeading>
      <p className="text-xs text-text-dim mb-3">{t('personalKeysDescription')}</p>

      {secrets === null ? (
        <p className={['text-sm', loadError ? 'text-red' : 'text-text-dim'].join(' ')}>
          {loadError || t('loading')}
        </p>
      ) : (
        <>
          {expanded === NEW_PANEL ? (
            <div className="py-4 border-b border-border-soft">
              <SecretForm
                key="new"
                initial={null}
                disabled={busy === NEW_PANEL}
                error={msg?.id === NEW_PANEL ? msg.text : null}
                onSave={body => void create(body)}
                onCancel={closeForm}
              />
            </div>
          ) : (
            <Button variant="primary" onClick={() => { setExpanded(NEW_PANEL); setMsg(null); }}>
              {t('addSecretKey')}
            </Button>
          )}

          <div className="flex flex-col gap-0.5 mt-3">
            {secrets.length === 0 && <p className="text-sm text-text-dim">{t('noSecretsYet')}</p>}
            {secrets.map(row => {
              const open = expanded === row.name;
              return (
                <section
                  key={row.name}
                  className={`hover:bg-surface2 transition-colors duration-150 ${open ? 'bg-surface2' : 'bg-surface'}`}
                >
                  <div className="py-2.5 px-3.5 flex items-center gap-3">
                    <IconButton size="sm" onClick={() => { setExpanded(open ? null : row.name); setMsg(null); }}>
                      {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </IconButton>
                    <span className="text-sm font-medium font-mono text-text truncate">{row.name}</span>
                    <span className="text-xs text-text-dim font-mono truncate whitespace-nowrap">
                      {row.hosts.join(' · ')}
                    </span>
                    <span className="flex-1" />
                    <FieldCheckbox
                      checked={row.enabled}
                      onChange={enabled => void toggleEnabled(row, enabled)}
                      label=""
                      title={t('secretEnabled')}
                      disabled={busy === row.name}
                    />
                    <IconButton
                      size="sm"
                      danger
                      filled={armedDelete === row.name}
                      title={t('delete')}
                      onClick={() => deleteArmed.handleClick(row.name, () => void remove(row))}
                      onMouseLeave={deleteArmed.disarm}
                      disabled={busy === row.name}
                    >
                      <Trash2 size={14} />
                    </IconButton>
                  </div>
                  {row.description && (
                    <p className="text-xs text-text-dim truncate pl-12 pr-3.5 pb-2 -mt-1">{row.description}</p>
                  )}
                  {open && (
                    <div className="border-t border-border py-2 px-3.5">
                      <SecretForm
                        key={row.name}
                        initial={row}
                        disabled={busy === row.name}
                        error={msg?.id === row.name ? msg.text : null}
                        onSave={body => void save(row, body)}
                        onCancel={closeForm}
                      />
                    </div>
                  )}
                  {!row.is_set && !open && (
                    <p className="text-xs text-red truncate pl-12 pr-3.5 pb-2 -mt-1">{t('secretReenter')}</p>
                  )}
                  {msg?.id === row.name && !open && <p className="text-xs px-3.5 pb-2 text-red">{msg.text}</p>}
                </section>
              );
            })}
          </div>
        </>
      )}

      {deletedFlash && <p className="text-xs mt-1 text-green">{t('secretDeleted')}</p>}
    </div>
  );
}
