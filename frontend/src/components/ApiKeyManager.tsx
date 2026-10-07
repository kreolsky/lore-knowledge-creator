/**
 * API key management view — the SINGLE issuance surface for every capability
 * mix. Rendered inside the unified Access tab (AccessPanel.tsx).
 *
 * ARCH: one key row carries a capabilities set (widget = notes + file uploads
 * on this doc — see SYSTEM: inbox, agent = MCP/Tool-API sandboxed to this doc's
 * subtree). The server
 * returns only the raw `lore_…` token; the widget's base64 {url, token}
 * envelope is copy-time packaging built client-side ("Copy widget key"), never
 * a second key format. After create/delete the project documents are refetched
 * so the doc-tree key indicator stays server-derived.
 */

import { useState, useEffect, useRef } from 'react';
import { Trash2, Copy, Check, Pencil, KeyRound, Plus } from 'lucide-react';
import { apiClient, HttpError } from '../api/client';
import { ApiKey } from '../types';
import { Button, FieldCheckbox, FieldInput, IconButton } from './ui';
import { useTranslation } from '../i18n';
import { useArmedAction } from '../hooks/useArmedAction';
import { withOptimistic } from '../utils/optimistic';
import { useAppStore } from '../store/app-store';

function KeyDeleteButton({ onDelete }: { onDelete: () => void }) {
  const { t } = useTranslation();
  const deleteAction = useArmedAction();
  return (
    <IconButton
      size="sm"
      danger
      filled={deleteAction.armed}
      title={t('deleteKey')}
      onClick={() => deleteAction.handleClick(onDelete)}
      onMouseLeave={deleteAction.disarm}
    >
      <Trash2 size={12} />
    </IconButton>
  );
}

interface Props {
  documentId: string;
}

interface FreshKey {
  keyId: string;
  token: string;
  capabilities: string[];
}

export default function ApiKeyManager({ documentId }: Props) {
  const { t } = useTranslation();
  // WHY: a ref, not a dep — the load runs on mount/route change, never on a language switch.
  const tRef = useRef(t);
  tRef.current = t;
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  // The raw token is the single canonical secret — both copy actions derive from it.
  const [freshKey, setFreshKey] = useState<FreshKey | null>(null);
  const [copied, setCopied] = useState<'widget' | 'token' | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const showToast = useAppStore(s => s.showToast);

  // Create form state.
  const [formOpen, setFormOpen] = useState(false);
  const [label, setLabel] = useState('');
  const [capWidget, setCapWidget] = useState(true);
  const [capAgent, setCapAgent] = useState(false);
  const [autoApply, setAutoApply] = useState(false);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    setLoading(true);
    apiClient.get(`/api-keys?document_id=${documentId}`)
      .then(setKeys)
      .catch((err) => {
        console.error('Failed to load API keys', err);
        showToast(tRef.current('failedToLoadApiKeys'), 'error');
      })
      .finally(() => setLoading(false));
  }, [documentId, showToast]);

  /** Copy-time packaging for the external widget app: base64 {url, token}. */
  const buildWidgetEnvelope = (token: string): string =>
    btoa(JSON.stringify({ url: window.location.origin, token }));

  /** The doc-tree key indicator is server-derived — refetch documents after
   * create/delete so it reflects the new key state. */
  const refreshDocuments = async () => {
    const proj = useAppStore.getState().currentProject;
    if (!proj) return;
    try {
      const data = await apiClient.get(`/projects/${proj.project_id}`);
      useAppStore.getState().setDocuments(data.documents);
    } catch {
      showToast(t('keyTreeRefreshFailed'));
    }
  };

  const handleCreate = async () => {
    if (creating || (!capWidget && !capAgent)) return;
    const capabilities = [
      ...(capWidget ? ['widget'] : []),
      ...(capAgent ? ['agent'] : []),
    ];
    setCreating(true);
    try {
      const res = await apiClient.post('/api-keys', {
        document_id: documentId,
        label: label.trim(),
        capabilities,
        auto_apply: capAgent && autoApply,
      });
      setFreshKey({ keyId: res.key_id, token: res.token, capabilities: res.capabilities });
      setCopied(null);
      setKeys(prev => [{
        key_id: res.key_id,
        label: res.label,
        document_id: res.document_id,
        capabilities: res.capabilities,
        auto_apply: res.auto_apply,
        created_at: res.created_at,
        last_used_at: null,
      }, ...prev]);
      setFormOpen(false);
      setLabel('');
      refreshDocuments();
    } catch (e) {
      // INVARIANT (no silent degradation): surface the backend reason. 422 =
      // a system document cannot be an agent scope root.
      if (e instanceof HttpError && e.status === 422) {
        showToast(t('agentKeyScopeSystemRejected'));
      } else {
        showToast(t('createKeyFailed'));
      }
    } finally {
      setCreating(false);
    }
  };

  const handleCopy = async (kind: 'widget' | 'token') => {
    if (!freshKey) return;
    const payload = kind === 'widget' ? buildWidgetEnvelope(freshKey.token) : freshKey.token;
    await navigator.clipboard.writeText(payload);
    showToast(t('copied'), 'info');
    setCopied(kind);
    setTimeout(() => setCopied(null), 2000);
  };

  const handleDelete = async (keyId: string) => {
    try {
      await apiClient.delete(`/api-keys/${keyId}`);
    } catch {
      showToast(t('deleteKeyFailed'));
      return;
    }
    setKeys(prev => prev.filter(k => k.key_id !== keyId));
    if (freshKey?.keyId === keyId) setFreshKey(null);
    refreshDocuments();
  };

  const handleRename = async (k: ApiKey) => {
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === k.label) { setRenamingId(null); return; }
    setRenamingId(null);
    await withOptimistic(
      { ...k, label: trimmed },
      k,
      (updated) => setKeys(prev => prev.map(key => key.key_id === k.key_id ? updated : key)),
      () => apiClient.patch(`/api-keys/${k.key_id}`, { label: trimmed }),
    );
  };

  const formatDateTime = (iso: string) => {
    try {
      const d = new Date(iso);
      return d.toLocaleDateString('ru-RU', {
        day: '2-digit', month: '2-digit', year: '2-digit',
        hour: '2-digit', minute: '2-digit',
      });
    } catch { return iso; }
  };

  if (loading) {
    return <div className="p-4 text-ui-base text-text-dim text-center">{t('loading')}</div>;
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Create-key affordance. ARCH (plan "iridescent-wibbling-heron"): the
       * manager owns its own visible trigger now — it used to rely on a hidden
       * <button data-create-key> clicked externally via document.querySelector
       * from the ReferencesPanel header. With the move to the unified Access
       * tab the manager must be self-contained, so the trigger is visible here. */}
      {!formOpen && (
        <Button variant="dashed" size="sm" onClick={() => setFormOpen(true)}>
          <Plus size={13} />
          {t('createKey')}
        </Button>
      )}

      {formOpen && (
        <div className="bg-surface2 border border-border-soft p-3 flex flex-col gap-2">
          <FieldInput
            value={label}
            onChange={e => setLabel(e.target.value)}
            placeholder={t('unnamedKey')}
            autoFocus
          />
          <FieldCheckbox checked={capWidget} onChange={setCapWidget} label={t('keyCapWidget')} />
          <FieldCheckbox checked={capAgent} onChange={setCapAgent} label={t('keyCapAgent')} />
          {capAgent && (
            <div className="pl-5">
              <FieldCheckbox checked={autoApply} onChange={setAutoApply} label={t('agentKeyAutoApply')} />
            </div>
          )}
          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={handleCreate}
              disabled={creating || (!capWidget && !capAgent)}
            >
              <KeyRound size={13} />
              {t('createKey')}
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setFormOpen(false)}>
              {t('cancel')}
            </Button>
          </div>
        </div>
      )}

      {/* Key list */}
      {keys.length === 0 && !formOpen && (
        <div className="py-4 px-2 text-ui-base text-text-dim text-center">
          {t('noApiKeysForDoc')}
        </div>
      )}

      {keys.map(k => {
        const isFresh = freshKey?.keyId === k.key_id;
        return (
          <div
            key={k.key_id}
            className="bg-surface2 border border-border-soft p-3 transition-all duration-150 hover:border-border hover:bg-surface3 cursor-default"
            style={isFresh ? {
              border: '1px solid var(--green)',
              background: 'var(--green-soft, rgba(34,197,94,0.08))',
            } : undefined}
          >
            <div className="flex justify-between items-center">
              <div className="flex-1 min-w-0">
                {renamingId === k.key_id ? (
                  <input
                    className="doc-rename-input"
                    data-rename-input
                    value={renameValue}
                    onChange={e => setRenameValue(e.target.value)}
                    autoFocus
                    onKeyDown={e => {
                      if (e.key === 'Enter') { e.preventDefault(); handleRename(k); }
                      if (e.key === 'Escape') setRenamingId(null);
                    }}
                    onBlur={() => handleRename(k)}
                    onClick={e => e.stopPropagation()}
                  />
                ) : (
                  <div className="text-ui-base font-medium flex items-center gap-1.5">
                    <span className="truncate">{k.label || t('unnamedKey')}</span>
                    {k.capabilities?.includes('widget') && (
                      <span className="shrink-0 text-ui-2xs px-1 py-0.5 bg-[var(--blue)] text-white">
                        {t('keyBadgeWidget')}
                      </span>
                    )}
                    {k.capabilities?.includes('agent') && (
                      <span className="shrink-0 text-ui-2xs px-1 py-0.5 bg-[var(--red)] text-white">
                        {t('keyBadgeAgent')}
                        {k.auto_apply ? ' · rw' : ' · ro'}
                      </span>
                    )}
                  </div>
                )}
                <div className="text-ui-xs text-text-dim mt-0.5">
                  Created {formatDateTime(k.created_at)}
                  {k.last_used_at && <> · Used {formatDateTime(k.last_used_at)}</>}
                </div>
                {isFresh && (
                  <div className="text-ui-2xs text-green mt-0.5">
                    {t('agentKeyTokenShownOnce')}
                  </div>
                )}
              </div>
              <div className="flex items-center gap-0.5 shrink-0">
                <IconButton
                  size="sm"
                  title={t('rename')}
                  onClick={() => { setRenamingId(k.key_id); setRenameValue(k.label); }}
                >
                  <Pencil size={13} />
                </IconButton>
                <KeyDeleteButton onDelete={() => handleDelete(k.key_id)} />
              </div>
            </div>
            {isFresh && (
              <div className="flex items-center gap-1.5 mt-2">
                {freshKey!.capabilities.includes('widget') && (
                  <Button variant="ghost" size="sm" onClick={() => handleCopy('widget')}>
                    {copied === 'widget' ? <Check size={13} /> : <Copy size={13} />}
                    {t('copyWidgetKey')}
                  </Button>
                )}
                {freshKey!.capabilities.includes('agent') && (
                  <Button variant="ghost" size="sm" onClick={() => handleCopy('token')}>
                    {copied === 'token' ? <Check size={13} /> : <Copy size={13} />}
                    {t('copyToken')}
                  </Button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
