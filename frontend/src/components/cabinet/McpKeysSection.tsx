/** Cabinet section: the MCP servers available to this user (SYSTEM: mcp-servers).
 *
 * A personal server takes one write-only key; Save stores it and immediately runs
 * the connection check, so a wrong key never looks like success. Shared and keyless
 * servers need nothing from the user. Every server that can ride has a toggle, and
 * a server the user can no longer see offers only the deletion of their key.
 */

import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import type { McpCheckResult } from '../../api/admin';
import { cabinetFetch } from '../../api/cabinet';
import { createArmedAction, type ArmedActionCore } from '../../hooks/useArmedAction';
import { Button, CenterHeading, FieldCheckbox, FieldInput, IconButton } from '../ui';
import { McpCheckLine } from '../McpCheckLine';
import { useTranslation } from '../../i18n';

/** One server from GET /api/cabinet/mcp.
 * INVARIANT(security): the key is a write-only field; the API answers
 * is_set / needs_reenter, never a value.
 * Why: the browser is outside the vault's trust boundary — chat frames relay
 * payloads toward the model, so a value echoed here would reach it (the same
 * exposure the SecretRow INVARIANT in PersonalKeysSection states). */
interface McpServerRow {
  id: string;
  name: string;
  title: string;
  host: string;
  auth_mode: 'personal' | 'shared' | 'none';
  help_url: string | null;
  created_by: string;
  created_by_name: string;
  /** The user holds a key for a server they can no longer see: delete-only. */
  unavailable: boolean;
  enabled: boolean;
  /** Personal: the user's key serves the current host. Shared: the admin's key
   * does. Null for a keyless server. */
  is_set: boolean | null;
  /** A key exists but does not serve the CURRENT host (the url moved) or no
   * longer decrypts: it must be re-entered. */
  needs_reenter: boolean;
}

/** One card's check: `result` null while it runs. */
interface CheckState {
  result: McpCheckResult | null;
  error?: string;
}

export function McpKeysSection() {
  const { t } = useTranslation();
  const [servers, setServers] = useState<McpServerRow[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [msg, setMsg] = useState('');
  const [msgErr, setMsgErr] = useState(false);
  const [saving, setSaving] = useState(false);
  // Write-only drafts: server id → the typed key, dropped on a successful save.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [checks, setChecks] = useState<Record<string, CheckState>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [deleteArmedId, setDeleteArmedId] = useState<string | null>(null);
  const deleteArmedRef = useRef<ArmedActionCore<string> | null>(null);
  if (deleteArmedRef.current === null) {
    deleteArmedRef.current = createArmedAction<string>(2000, setDeleteArmedId);
  }
  const deleteArmed = deleteArmedRef.current;
  useEffect(() => () => deleteArmed.dispose(), [deleteArmed]);

  useEffect(() => {
    let alive = true;
    cabinetFetch('mcp', null, 'GET').then(([data, err]) => {
      if (!alive) return;
      if (err) {
        setLoadError(err);
        return;
      }
      setServers((data?.servers ?? []) as McpServerRow[]);
    });
    return () => { alive = false; };
  }, []);

  const showError = (text: string) => { setMsg(text); setMsgErr(true); };

  const flash = (text: string) => {
    setMsg(text);
    setMsgErr(false);
    setTimeout(() => setMsg(''), 2000);
  };

  const replaceServer = (entry: McpServerRow) =>
    setServers(prev => (prev ?? []).map(s => (s.id === entry.id ? entry : s)));

  const setCheck = (id: string, state: CheckState | null) =>
    setChecks(prev => {
      const { [id]: _dropped, ...rest } = prev;
      return state ? { ...rest, [id]: state } : rest;
    });

  const runCheck = async (server: McpServerRow) => {
    setCheck(server.id, { result: null });
    const [data, err] = await cabinetFetch(
      `mcp/${encodeURIComponent(server.id)}/check`, {}, 'POST',
    );
    setCheck(server.id, err ? { result: null, error: err } : { result: data as McpCheckResult });
  };

  const handleSave = async (server: McpServerRow) => {
    const value = drafts[server.id];
    if (!value) return;
    setSaving(true);
    setMsg('');
    const [data, err] = await cabinetFetch(
      `mcp/${encodeURIComponent(server.id)}/secrets`, { value }, 'PUT',
    );
    setSaving(false);
    if (err) { showError(err); return; }
    replaceServer(data.server as McpServerRow);
    setDrafts(prev => ({ ...prev, [server.id]: '' }));
    flash(t('cabinetMcpKeySaved'));
    await runCheck(server);
  };

  const handleToggle = async (server: McpServerRow, enabled: boolean) => {
    setSaving(true);
    setMsg('');
    const [data, err] = await cabinetFetch(
      `mcp/${encodeURIComponent(server.id)}`, { enabled }, 'PATCH',
    );
    setSaving(false);
    if (err) { showError(err); return; }
    replaceServer(data.server as McpServerRow);
  };

  const handleDelete = async (server: McpServerRow) => {
    setSaving(true);
    setMsg('');
    const [, err] = await cabinetFetch(
      `mcp/${encodeURIComponent(server.id)}/secrets`, null, 'DELETE',
    );
    setSaving(false);
    if (err) { showError(err); return; }
    // Local projection of the server's answer: no row → nothing set, nothing
    // to re-enter; an unavailable card existed only because of that row.
    setServers(prev => (server.unavailable
      ? (prev ?? []).filter(s => s.id !== server.id)
      : (prev ?? []).map(s => (s.id === server.id ? { ...s, is_set: false, needs_reenter: false } : s))));
    setDrafts(prev => ({ ...prev, [server.id]: '' }));
    setCheck(server.id, null);
    flash(t('secretDeleted'));
  };

  const renderKeyField = (server: McpServerRow) => (
    <div className="flex flex-col gap-1">
      {server.help_url && (
        <a
          className="text-xs text-accent underline self-start"
          href={server.help_url}
          target="_blank"
          rel="noopener noreferrer"
        >{t('cabinetMcpHelp')}</a>
      )}
      {server.needs_reenter && (
        <p className="text-xs text-red">
          {t('secretReenter')} — {t('cabinetMcpReenterHint')}
        </p>
      )}
      <div className="flex gap-2 items-center">
        <FieldInput
          onPanel
          className="flex-1"
          type="password"
          value={drafts[server.id] ?? ''}
          onChange={e => setDrafts(prev => ({ ...prev, [server.id]: e.target.value }))}
          placeholder={server.is_set ? t('secretKeepValue') : t('secretValuePlaceholder')}
          autoComplete="new-password"
          maxLength={8192}
          disabled={saving}
        />
        <Button
          variant="primary"
          onClick={() => handleSave(server)}
          disabled={saving || !drafts[server.id]}
        >
          {t('save')}
        </Button>
      </div>
    </div>
  );

  // A shared server without a working key must not read as connected.
  const renderNoKeyLine = (server: McpServerRow) => (
    server.is_set === false
      ? <p className="text-xs text-red">{t('cabinetMcpSharedMissing')}</p>
      : <p className="text-xs text-text-dim">{t('cabinetMcpNoKeyNeeded')}</p>
  );

  // WHY: only a state that needs the user's hand surfaces while collapsed; a
  // broken server must not read as healthy behind a closed row.
  const collapsedAlert = (server: McpServerRow): string | null => {
    if (server.unavailable) return t('cabinetMcpUnavailable');
    if (server.needs_reenter) return t('secretReenter');
    if (server.auth_mode !== 'personal' && server.is_set === false) return t('cabinetMcpSharedMissing');
    return null;
  };

  const renderCard = (server: McpServerRow) => {
    const personal = server.auth_mode === 'personal';
    const deletable = server.unavailable || (personal && (server.is_set || server.needs_reenter));
    const canRide = !server.unavailable && server.is_set !== false;
    const check = checks[server.id];
    const open = expanded === server.id;
    const alert = open ? null : collapsedAlert(server);
    return (
      <section
        key={server.id}
        className={`hover:bg-surface2 transition-colors duration-150 ${open ? 'bg-surface2' : 'bg-surface'}`}
      >
        <div className="py-2.5 px-3.5 flex items-center gap-3">
          <IconButton size="sm" onClick={() => setExpanded(open ? null : server.id)}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </IconButton>
          <span className="text-sm font-medium font-mono text-text truncate">{server.name}</span>
          {/* The row names the key's destination: the server's host and its creator. */}
          <span className="text-xs text-text-dim font-mono truncate whitespace-nowrap">
            {server.host}{server.created_by_name ? ` | ${server.created_by_name}` : ''}
          </span>
          <span className="flex-1" />
          {canRide && (
            <FieldCheckbox
              checked={server.enabled}
              onChange={enabled => handleToggle(server, enabled)}
              label=""
              title={t('secretEnabled')}
              disabled={saving}
            />
          )}
          {deletable && (
            <IconButton
              size="sm"
              danger
              filled={deleteArmedId === server.id}
              title={t('delete')}
              onClick={() => deleteArmed.handleClick(server.id, () => handleDelete(server))}
              onMouseLeave={deleteArmed.disarm}
              disabled={saving}
            >
              <Trash2 size={13} />
            </IconButton>
          )}
        </div>
        {server.title !== server.name && (
          <p className="text-xs text-text-dim truncate pl-12 pr-3.5 pb-2 -mt-1">{server.title}</p>
        )}
        {alert && <p className="text-xs text-red truncate pl-12 pr-3.5 pb-2 -mt-1">{alert}</p>}
        {open && (
          <div className="border-t border-border py-2 px-3.5">
            {server.unavailable && <p className="text-xs text-red">{t('cabinetMcpUnavailable')}</p>}
            {!server.unavailable && (personal ? renderKeyField(server) : renderNoKeyLine(server))}
            {check && <div className="mt-1.5"><McpCheckLine result={check.result} error={check.error} /></div>}
          </div>
        )}
      </section>
    );
  };

  return (
    <div>
      <CenterHeading>{t('cabinetMcpServers')}</CenterHeading>
      <p className="text-xs text-text-dim mb-3">{t('cabinetMcpDescription')}</p>
      {servers === null ? (
        <p className={['text-sm', loadError ? 'text-red' : 'text-text-dim'].join(' ')}>
          {loadError || t('loading')}
        </p>
      ) : (
        <div className="flex flex-col gap-0.5">
          {servers.length === 0 && <p className="text-sm text-text-dim">{t('cabinetMcpEmpty')}</p>}
          {servers.map(renderCard)}
        </div>
      )}
      {msg && (
        <p className={['text-xs mt-1', msgErr ? 'text-red' : 'text-green'].join(' ')}>{msg}</p>
      )}
    </div>
  );
}
