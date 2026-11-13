/** Admin / moderator: MCP server definitions (SYSTEM: mcp-servers).
 *
 * One card per row of GET /api/admin/mcp-servers — the moderator's list is
 * already scoped server-side to their own single-subject servers, and the
 * subjects editor is admin-only (a moderator's server is born, lives and dies
 * bound to their virtual group). The form asks for a name, a url and who holds
 * the key: each user (entered in the cabinet), one shared write-only key, or
 * none; the auth scheme waits in a collapsed Advanced block. The Public checkbox
 * + group checkboxes reuse the model-access subject ladder. Every save sends the
 * full definition and replaces the row with the server's answer; a refusal
 * renders under the form and keeps the draft.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { ArmedDeleteButton, Button, CenterHeading, FieldCheckbox, FieldInput, IconButton, SegmentedControl } from '../ui';
import {
  checkAdminMcpServer, createAdminMcpServer, deleteAdminMcpServer, listAdminGroups, listAdminMcpServers,
  patchAdminMcpServer, PUBLIC_SUBJECT, type AdminGroup, type AdminMcpServer,
  type AdminMcpServerBody, type McpAuthMode, type McpCheckResult,
} from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';
import { McpCheckLine } from '../McpCheckLine';
import { groupLabel } from './GroupsSection';

type SchemePreset = 'bearer' | 'raw' | 'custom';

/** `auth_scheme` → preset: 'Bearer' / '' (key as is) / anything else = custom. */
function deriveSchemePreset(scheme: string): SchemePreset {
  if (scheme === 'Bearer') return 'bearer';
  return scheme === '' ? 'raw' : 'custom';
}

/** Create/edit form — one instance at a time (the create panel or one
 * expanded card); `key` at the mount site resets it per server. For a
 * moderator the subjects block is a fixed-scope hint (they may not pick
 * subjects; create sends their own group subject, edit sends none). */
function ServerForm({ initial, isAdmin, ownSubject, groups, disabled, error, onSave, onCancel }: {
  initial: AdminMcpServer | null;
  isAdmin: boolean;
  /** `mod:<uid>` — the only subject a moderator may create with. */
  ownSubject: string | null;
  groups: AdminGroup[];
  disabled: boolean;
  error: string | null;
  onSave: (body: AdminMcpServerBody) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initial?.name ?? '');
  const [title, setTitle] = useState(initial?.title ?? '');
  const [url, setUrl] = useState(initial?.url ?? '');
  const [authMode, setAuthMode] = useState<McpAuthMode>(initial?.auth_mode ?? 'personal');
  const [helpUrl, setHelpUrl] = useState(initial?.help_url ?? '');
  const [autoApprove, setAutoApprove] = useState(initial?.auto_approve ?? false);
  // Write-only draft: never prefilled, omitted from the body when empty.
  const [sharedKey, setSharedKey] = useState('');
  // Scheme preset is derived from the stored prefix, never stored itself.
  const [schemePreset, setSchemePreset] = useState<SchemePreset>(deriveSchemePreset(initial?.auth_scheme ?? 'Bearer'));
  // Custom starts empty (placeholder shows the shape), same as the keys form.
  const [scheme, setScheme] = useState(() => (schemePreset === 'custom' ? initial?.auth_scheme ?? '' : ''));
  // A non-default scheme must not hide behind a closed toggle.
  const [advancedOpen, setAdvancedOpen] = useState(schemePreset !== 'bearer');
  const [check, setCheck] = useState<{ result: McpCheckResult | null; error?: string } | null>(null);
  const [subjects, setSubjects] = useState<string[]>(
    initial?.subjects ?? (isAdmin ? [PUBLIC_SUBJECT] : []),
  );

  const toggleSubject = (subject: string, on: boolean) =>
    setSubjects(prev => (on ? [...prev, subject] : prev.filter(s => s !== subject)));

  const save = () => {
    onSave({
      name: name.trim(),
      title: title.trim(),
      url: url.trim(),
      auth_mode: authMode,
      auth_scheme: { bearer: 'Bearer', raw: '', custom: scheme.trim() }[schemePreset],
      help_url: helpUrl.trim(),
      auto_approve: autoApprove,
      ...(authMode === 'shared' && sharedKey ? { shared_key: sharedKey } : {}),
      // Create always names a subject (the backend defaults to public, which
      // a moderator may not pick); edit sends subjects for admins only — a
      // moderator's subjects are untouchable.
      subjects: isAdmin
        ? subjects
        : initial
          ? undefined
          : [ownSubject!],
    });
  };

  // The check dials the SAVED row, so it is offered only while the form's mode
  // matches it; a personal server is each user's to check, from the cabinet.
  const canCheck = !!initial && initial.auth_mode !== 'personal' && authMode === initial.auth_mode;
  const runCheck = async () => {
    if (!initial) return;
    setCheck({ result: null });
    try {
      setCheck({ result: await checkAdminMcpServer(initial.id) });
    } catch (err: unknown) {
      setCheck({ result: null, error: serverRefusalDetail(err) ?? t('adminMcpCheckFailed') });
    }
  };

  const sharedPlaceholder = initial?.shared_key_set ? t('secretKeepValue') : t('secretValuePlaceholder');

  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)] gap-2">
        <FieldInput
          onPanel
          className="font-mono"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder={t('adminMcpNamePlaceholder')}
          maxLength={32}
          disabled={disabled}
        />
        <FieldInput
          onPanel
          value={title}
          onChange={e => setTitle(e.target.value)}
          placeholder={t('adminMcpDescriptionPlaceholder')}
          maxLength={200}
          disabled={disabled}
        />
      </div>
      <FieldInput
        onPanel
        className="font-mono"
        value={url}
        onChange={e => setUrl(e.target.value)}
        placeholder={t('adminMcpUrlPlaceholder')}
        disabled={disabled}
      />
      <p className="text-xs text-text-dim mt-1">{t('adminMcpModeHint')}</p>
      <div>
        <SegmentedControl
          ariaLabel={t('adminMcpModeHint')}
          options={[
            { value: 'personal', label: t('adminMcpModePersonal') },
            { value: 'shared', label: t('adminMcpModeShared') },
            { value: 'none', label: t('adminMcpModeNone') },
          ]}
          value={authMode}
          onChange={mode => { setAuthMode(mode as McpAuthMode); setCheck(null); }}
          disabled={disabled}
        />
      </div>
      {authMode === 'personal' && (
        <FieldInput
          onPanel
          className="font-mono"
          value={helpUrl}
          onChange={e => setHelpUrl(e.target.value)}
          placeholder={t('adminMcpHelpUrlPlaceholder')}
          disabled={disabled}
        />
      )}
      {authMode === 'shared' && (
        <>
          {initial?.shared_needs_reenter && (
            <p className="text-xs text-red">{t('secretReenter')} — {t('adminMcpSharedReenterHint')}</p>
          )}
          <FieldInput
            onPanel
            type="password"
            value={sharedKey}
            onChange={e => setSharedKey(e.target.value)}
            placeholder={initial?.shared_needs_reenter ? t('secretReenter') : sharedPlaceholder}
            autoComplete="new-password"
            maxLength={8192}
            disabled={disabled}
          />
        </>
      )}
      {authMode !== 'none' && (
        <>
          <div>
            <Button onClick={() => setAdvancedOpen(open => !open)} disabled={disabled}>
              {advancedOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              {t('adminMcpAdvanced')}
            </Button>
          </div>
          {advancedOpen && (
            <>
              <p className="text-xs text-text-dim">{t('adminMcpSchemeHint')}</p>
              <div>
                <SegmentedControl
                  ariaLabel={t('adminMcpSchemeHint')}
                  options={[
                    { value: 'bearer', label: 'Bearer' },
                    { value: 'raw', label: t('adminMcpSchemeRaw') },
                    { value: 'custom', label: t('adminMcpSchemeCustom') },
                  ]}
                  value={schemePreset}
                  onChange={p => setSchemePreset(p as SchemePreset)}
                  disabled={disabled}
                />
              </div>
              {schemePreset === 'custom' && (
                <FieldInput
                  onPanel
                  className="font-mono"
                  value={scheme}
                  onChange={e => setScheme(e.target.value)}
                  placeholder={t('adminMcpSchemePlaceholder')}
                  maxLength={32}
                  disabled={disabled}
                />
              )}
              {schemePreset === 'custom' && (
                <p className="text-xs text-text-dim -mt-1">{t('adminMcpSchemeCustomHint')}</p>
              )}
            </>
          )}
        </>
      )}

      <FieldCheckbox
        checked={autoApprove}
        onChange={setAutoApprove}
        label={t('adminMcpAutoApprove')}
        disabled={disabled}
      />
      <p className="text-xs text-text-dim -mt-1">{t('adminMcpAutoApproveHint')}</p>

      {isAdmin ? (
        <>
          <p className="text-xs text-text-dim mt-1">{t('adminMcpSubjectsHint')}</p>
          <div className="flex flex-col gap-1.5">
            <FieldCheckbox
              checked={subjects.includes(PUBLIC_SUBJECT)}
              onChange={on => toggleSubject(PUBLIC_SUBJECT, on)}
              label={t('adminModelPublic')}
              disabled={disabled}
            />
            {groups.map(g => (
              <FieldCheckbox
                key={g.subject}
                checked={subjects.includes(g.subject)}
                onChange={on => toggleSubject(g.subject, on)}
                label={groupLabel(g, t)}
                disabled={disabled}
              />
            ))}
          </div>
        </>
      ) : (
        <p className="text-xs text-text-dim mt-1">{t('adminMcpModeratorScope')}</p>
      )}

      <div className="flex gap-2 items-center">
        <Button
          variant="primary"
          onClick={save}
          disabled={disabled || !name.trim() || !url.trim()}
        >
          {t('save')}
        </Button>
        <Button onClick={onCancel} disabled={disabled}>{t('cancel')}</Button>
        {canCheck && (
          <Button onClick={() => void runCheck()} disabled={disabled || (check !== null && check.result === null && !check.error)}>
            {t('adminMcpCheck')}
          </Button>
        )}
        {error && <span className="text-xs text-red">{error}</span>}
      </div>
      {check && <McpCheckLine result={check.result} error={check.error} />}
    </div>
  );
}

export function McpServersSection() {
  const { t } = useTranslation();
  const currentUser = useAppStore(s => s.currentUser);
  const isAdmin = currentUser?.is_admin ?? false;
  const ownSubject = isAdmin ? null : `mod:${currentUser?.user_id ?? ''}`;
  const [servers, setServers] = useState<AdminMcpServer[] | null>(null);
  const [groups, setGroups] = useState<AdminGroup[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ id: string; text: string } | null>(null);
  const tRef = useRef(t);
  tRef.current = t;

  const reload = useCallback(async () => {
    try {
      const rows = await listAdminMcpServers();
      setServers(rows);
      // /admin/groups is require_admin — a moderator's page must not fire it.
      if (currentUser?.is_admin) {
        try {
          setGroups(await listAdminGroups());
        } catch {
          setGroups([]);
        }
      }
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      useAppStore.getState().showToast(tRef.current('adminListLoadFailed'), 'error');
    }
  }, [currentUser?.is_admin]);

  useEffect(() => { void reload(); }, [reload]);

  const replaceRow = (row: AdminMcpServer) =>
    setServers(prev => prev?.map(s => (s.id === row.id ? row : s)) ?? prev);

  const create = async (body: AdminMcpServerBody) => {
    setBusy('new');
    setMsg(null);
    try {
      const row = await createAdminMcpServer(body);
      setServers(prev => [...(prev ?? []), row].sort((a, b) => a.name.localeCompare(b.name)));
      setExpanded(null);
    } catch (err: unknown) {
      setMsg({ id: 'new', text: serverRefusalDetail(err) ?? t('adminMcpSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const save = async (server: AdminMcpServer, body: AdminMcpServerBody) => {
    setBusy(server.id);
    setMsg(null);
    try {
      replaceRow(await patchAdminMcpServer(server.id, body));
      setExpanded(null);
    } catch (err: unknown) {
      setMsg({ id: server.id, text: serverRefusalDetail(err) ?? t('adminMcpSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const toggleEnabled = async (server: AdminMcpServer, enabled: boolean) => {
    if (busy) return;
    setBusy(server.id);
    setMsg(null);
    setServers(prev => prev?.map(s => (s.id === server.id ? { ...s, enabled } : s)) ?? prev);
    try {
      replaceRow(await patchAdminMcpServer(server.id, { enabled }));
    } catch (err: unknown) {
      setServers(prev => prev?.map(s => (s.id === server.id ? server : s)) ?? prev);
      setMsg({ id: server.id, text: serverRefusalDetail(err) ?? t('adminMcpSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const remove = async (server: AdminMcpServer) => {
    setBusy(server.id);
    setMsg(null);
    try {
      await deleteAdminMcpServer(server.id);
      setServers(prev => prev?.filter(s => s.id !== server.id) ?? prev);
    } catch (err: unknown) {
      setMsg({ id: server.id, text: serverRefusalDetail(err) ?? t('adminMcpSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const createOpen = expanded === 'new';

  return (
    <>
      {/* Variant of the admin heading: mb-1 — the hint hugs the rule. */}
      <CenterHeading className="text-ui-md font-semibold text-text pb-1.5 border-b border-border mb-1">{t('adminMcpServers')}</CenterHeading>
      <p className="text-xs text-text-dim mb-3">{t('adminMcpServersHint')}</p>
      {loadFailed && <p className="text-xs text-red mb-2">{t('adminListLoadFailed')}</p>}

      {createOpen ? (
        // Same create surface as the Skills section: bare py-4 block under a
        // soft rule (no panel), mirroring its primary/lg "new" button above.
        <div className="py-4 border-b border-border-soft">
          <ServerForm
            key="new"
            initial={null}
            isAdmin={isAdmin}
            ownSubject={ownSubject}
            groups={groups}
            disabled={busy === 'new'}
            error={msg?.id === 'new' ? msg.text : null}
            onSave={body => void create(body)}
            onCancel={() => { setExpanded(null); setMsg(null); }}
          />
        </div>
      ) : (
        <Button variant="primary" onClick={() => { setExpanded('new'); setMsg(null); }}>
          {t('adminMcpAdd')}
        </Button>
      )}

      <div className="flex flex-col gap-0.5 mt-3">
        {servers?.length === 0 && <p className="text-sm text-text-dim">{t('adminMcpNoServers')}</p>}
        {(servers ?? []).map(server => {
          const open = expanded === server.id;
          return (
            <section
              key={server.id}
              data-mcp-id={server.id}
              className={`hover:bg-surface2 transition-colors duration-150 ${open ? 'bg-surface2' : 'bg-surface'}`}
            >
              <div className="py-2.5 px-3.5 flex items-center gap-3">
                <IconButton size="sm" onClick={() => setExpanded(open ? null : server.id)}>
                  {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </IconButton>
                <span className="text-sm font-medium font-mono text-text truncate">{server.name}</span>
                <span className="text-xs text-text-dim font-mono truncate whitespace-nowrap">
                  {server.host}{server.created_by_name ? ` | ${server.created_by_name}` : ''}
                </span>
                {server.auto_approve && (
                  <span className="text-xs text-text-dim whitespace-nowrap">{t('adminMcpAutoApproveBadge')}</span>
                )}
                <span className="flex-1" />
                <FieldCheckbox
                  checked={server.enabled}
                  onChange={enabled => void toggleEnabled(server, enabled)}
                  label=""
                  title={t('adminMcpEnabled')}
                  disabled={busy === server.id}
                />
                <ArmedDeleteButton
                  disabled={busy === server.id}
                  onFire={() => void remove(server)}
                />
              </div>
              {server.title !== server.name && (
                <p className="text-xs text-text-dim truncate pl-12 pr-3.5 pb-2 -mt-1">{server.title}</p>
              )}
              {open && (
                // Same expanded-panel shape as the Users section's card:
                // border-t separator + the row's px-3.5 (no extra indent).
                <div className="border-t border-border py-2 px-3.5">
                  <ServerForm
                    key={server.id}
                    initial={server}
                    isAdmin={isAdmin}
                    ownSubject={ownSubject}
                    groups={groups}
                    disabled={busy === server.id}
                    error={msg?.id === server.id ? msg.text : null}
                    onSave={body => void save(server, body)}
                    onCancel={() => { setExpanded(null); setMsg(null); }}
                  />
                </div>
              )}
              {server.shared_needs_reenter && !open && (
                <p className="text-xs text-red truncate pl-12 pr-3.5 pb-2 -mt-1">{t('secretReenter')}</p>
              )}
              {msg?.id === server.id && !open && <p className="text-xs px-3.5 pb-2 text-red">{msg.text}</p>}
            </section>
          );
        })}
      </div>
    </>
  );
}
