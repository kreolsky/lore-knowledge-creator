/**
 * Pipeline config manager — the "Pipelines" sub-panel in ReferencesPanel.
 * Local state, plus the chat store's utility model roster (toolModels ∩
 * structuredOutputModels) for the model dropdown.
 * Supports multiple agent configs per document, each with a trigger event and a
 * kind: an extractor (runs per reference) or a digest (runs per binding, reads
 * chats; a project root may pick whose). Every card autosaves once its config and
 * target documents are chosen; a draft card is deleted locally.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { Play, Trash2 } from 'lucide-react';
import { apiClient, ForbiddenError } from '../../api/client';
import { listMembers } from '../../api/access';
import { AgentConfig, ProjectMember } from '../../types';
import { useAppStore } from '../../store/app-store';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';
import { Button, FieldInput, FieldCheckbox, Dropdown, SegmentedControl } from '../ui';
import { ParentPickerPopup } from '../ParentPickerPopup';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useIsProjectOwner } from '../../hooks/useIsProjectOwner';

const TRIGGER_EVENTS = [
  { value: 'transcription_complete', label: 'transcription_complete' },
];

interface Props {
  documentId: string;
}

/** Extractor model options: the utility roster narrowed to structured-output
 * models (the extractor sends json_schema strict — a model without it fails the
 * run). A saved model outside that set stays listed, marked, so an existing
 * config is never silently shown as something else. */
export function extractorModelOptions(
  toolModels: string[], structuredOutputModels: string[], saved: string, unsupportedLabel: string,
): { value: string; label: string }[] {
  const structured = new Set(structuredOutputModels);
  const ids = toolModels.filter(m => structured.has(m));
  const options = ids.map(m => ({ value: m, label: m }));
  if (saved && !ids.includes(saved)) options.push({ value: saved, label: `${saved} — ${unsupportedLabel}` });
  return options;
}

type AgentKind = 'extractor' | 'digest';

const EXTRACTOR_TITLE_TEMPLATE = 'extractor-{yyyy}-{mm}-{dd} {HH}:{MM} {rnd:4}';
// WHY: empty sends null, so the backend's digest default title applies.
const DIGEST_TITLE_TEMPLATE = '';
// WHY: a debounce, so typing a title template is one save, not one per keystroke.
const AUTOSAVE_DELAY_MS = 700;

interface ConfigRow {
  /** Stable identity: rows are added, deleted and saved out of order. */
  key: string;
  config: AgentConfig | null;
  kind: AgentKind;
  selectedConfigDocId: string | null;
  selectedTargetDocId: string | null;
  triggerEvent: string;
  titleTemplate: string;
  modelOverride: string;
  /** Digest only; null = the creator alone. */
  subjectUserIds: string[] | null;
  /** Edit counter; the row is unsaved while `rev !== savedRev`. */
  rev: number;
  savedRev: number;
  saving: boolean;
}

let rowSeq = 0;
const nextRowKey = () => `row-${++rowSeq}`;

function makeEmptyRow(): ConfigRow {
  return {
    key: nextRowKey(),
    config: null,
    kind: 'extractor',
    selectedConfigDocId: null,
    selectedTargetDocId: null,
    triggerEvent: 'transcription_complete',
    titleTemplate: EXTRACTOR_TITLE_TEMPLATE,
    modelOverride: '',
    subjectUserIds: null,
    rev: 0,
    savedRev: 0,
    saving: false,
  };
}

function rowFromConfig(c: AgentConfig): ConfigRow {
  const kind: AgentKind = c.kind === 'digest' ? 'digest' : 'extractor';
  return {
    key: nextRowKey(),
    config: c,
    kind,
    selectedConfigDocId: c.config_doc_id,
    selectedTargetDocId: c.target_doc_id,
    triggerEvent: c.trigger_event || 'transcription_complete',
    titleTemplate: c.title_template || (kind === 'digest' ? DIGEST_TITLE_TEMPLATE : EXTRACTOR_TITLE_TEMPLATE),
    modelOverride: c.model || '',
    subjectUserIds: c.subject_user_ids ?? null,
    rev: 0,
    savedRev: 0,
    saving: false,
  };
}

/** A row the autosave should write now: edited since its last save, not mid-save,
 * and complete enough for the backend (both documents chosen). */
export function needsAutosave(row: ConfigRow): boolean {
  return row.rev !== row.savedRev && !row.saving && !!row.selectedConfigDocId && !!row.selectedTargetDocId;
}

/** Whether this viewer may choose whose chats a digest binding reads. Mirrors
 * the backend gate (`resolve_digest_subjects`): a project root, on a binding
 * that is new or that they created — the creator is never reassigned. */
export function canPickDigestSubjects(isRoot: boolean, config: AgentConfig | null, userId: string | undefined): boolean {
  if (!isRoot || !userId) return false;
  return !config || config.created_by === userId;
}

/** Whether this viewer may change or run a digest binding: its author or a project
 * root (backend `require_digest_editor`). Anyone with full access may still delete it. */
export function canEditDigestBinding(isRoot: boolean, config: AgentConfig | null, userId: string | undefined): boolean {
  return !config || isRoot || (!!userId && config.created_by === userId);
}

/** PUT /agent-config body. `subject_user_ids` is sent only by a viewer who may
 * pick it; null leaves the stored list as is. */
export function agentConfigPayload(documentId: string, row: ConfigRow, pickSubjects: boolean) {
  return {
    document_id: documentId,
    config_doc_id: row.selectedConfigDocId,
    target_doc_id: row.selectedTargetDocId,
    trigger_event: row.triggerEvent,
    title_template: row.titleTemplate || null,
    model: row.modelOverride || null,
    kind: row.kind,
    subject_user_ids: row.kind === 'digest' && pickSubjects ? row.subjectUserIds : null,
  };
}

function DigestSubjectPicker({ members, selected, onChange }: {
  members: ProjectMember[]; selected: string[]; onChange: (ids: string[]) => void;
}) {
  const { t } = useTranslation();
  const toggle = (uid: string, on: boolean) =>
    onChange(on ? [...selected, uid] : selected.filter(id => id !== uid));
  return (
    <div className="flex flex-col gap-1" onClick={e => e.stopPropagation()}>
      <span className="text-ui-sm text-text-dim">{t('digestSubjects')}</span>
      {members.map(m => (
        <FieldCheckbox
          key={m.user_id}
          checked={selected.includes(m.user_id)}
          onChange={on => toggle(m.user_id, on)}
          label={m.name || m.email}
          className="text-ui-sm"
        />
      ))}
      <span className="text-ui-2xs text-text-muted">{t('digestSubjectsHint')}</span>
    </div>
  );
}

function AgentDeleteButton({ onDelete }: { onDelete: () => void }) {
  const deleteAction = useArmedAction();
  return (
    <Button
      variant={deleteAction.armed ? "danger" : "ghost"}
      size="sm"
      onClick={() => deleteAction.handleClick(onDelete)}
      onMouseLeave={deleteAction.disarm}
    >
      <Trash2 size={12} />
    </Button>
  );
}

export default function TranscriptionAgentConfigManager({ documentId }: Props) {
  const { t } = useTranslation();
  const documents = useAppStore(s => s.documents);
  const toolModels = useChatStore(s => s.toolModels);
  const structuredOutputModels = useChatStore(s => s.structuredOutputModels);
  const modelsLoaded = useChatStore(s => s.modelsLoaded);
  const loadModels = useChatStore(s => s.loadModels);
  const currentUserId = useAppStore(s => s.currentUser?.user_id);
  const projectId = useAppStore(s => s.currentProject?.project_id);
  const isRoot = useIsProjectOwner();
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [runningConfigId, setRunningConfigId] = useState<string | null>(null);
  const [rows, setRows] = useState<ConfigRow[]>([makeEmptyRow()]);
  const [loading, setLoading] = useState(true);
  const [pickerAnchor, setPickerAnchor] = useState<{ rowKey: string; type: 'config' | 'target'; rect: DOMRect } | null>(null);
  const showToast = useAppStore(s => s.showToast);
  // WHY: the autosave callback reads t through a ref, so a language switch does not re-arm the debounce.
  const tRef = useRef(t);
  tRef.current = t;

  useEffect(() => {
    if (!modelsLoaded) loadModels();
  }, [modelsLoaded, loadModels]);

  // The members endpoint is root-only, as is the subject picker it feeds.
  useEffect(() => {
    if (!isRoot || !projectId) return;
    let cancelled = false;
    listMembers(projectId)
      .then(list => { if (!cancelled) setMembers(list.filter(m => !m.pending && m.user_id)); })
      .catch(() => useAppStore.getState().showToast(t('digestMembersLoadFailed'), 'error'));
    return () => { cancelled = true; };
  }, [isRoot, projectId, t]);

  const loadConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiClient.get(`/agent-configs?document_id=${documentId}`);
      if (data && Array.isArray(data) && data.length > 0) {
        setRows(data.map((c: AgentConfig) => rowFromConfig(c)));
      } else {
        setRows([makeEmptyRow()]);
      }
    } catch {
      setRows([makeEmptyRow()]);
    } finally {
      setLoading(false);
    }
  }, [documentId]);

  useEffect(() => {
    loadConfigs();
  }, [loadConfigs]);

  const setRowState = (key: string, patch: Partial<ConfigRow>) => {
    setRows(prev => prev.map(r => r.key === key ? { ...r, ...patch } : r));
  };

  /** A user edit: marks the row unsaved, which the autosave below picks up. */
  const updateRow = (key: string, patch: Partial<ConfigRow>) => {
    setRows(prev => prev.map(r => r.key === key ? { ...r, ...patch, rev: r.rev + 1 } : r));
  };

  const saveRow = useCallback(async (row: ConfigRow) => {
    setRowState(row.key, { saving: true });
    try {
      const saved: AgentConfig = await apiClient.put(
        '/agent-config',
        agentConfigPayload(documentId, row, canPickDigestSubjects(isRoot, row.config, currentUserId)),
      );
      // An edit made while the request was in flight keeps rev ahead, so it saves next.
      setRowState(row.key, { config: saved, savedRev: row.rev, saving: false });
    } catch (err) {
      console.error('Failed to save pipeline config', err);
      // A 403 here is the digest subject gate: name it, a generic failure reads as a glitch.
      const forbidden = err instanceof ForbiddenError && row.kind === 'digest';
      showToast(tRef.current(forbidden ? 'digestSubjectsForbidden' : 'agentConfigSaveFailed'), 'error');
      // WHY: marked saved so a refused write is not retried in a loop; the next edit retries it.
      setRowState(row.key, { savedRev: row.rev, saving: false });
    }
  }, [documentId, isRoot, currentUserId, showToast]);

  useEffect(() => {
    const due = rows.filter(r =>
      needsAutosave(r) && !(r.kind === 'digest' && !canEditDigestBinding(isRoot, r.config, currentUserId)));
    if (due.length === 0) return;
    const timer = setTimeout(() => due.forEach(saveRow), AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [rows, saveRow, isRoot, currentUserId]);

  const handleDelete = async (row: ConfigRow) => {
    if (!row.config) {
      setRows(prev => prev.filter(r => r.key !== row.key));
      return;
    }
    try {
      await apiClient.delete(`/agent-config/${row.config.config_id}`);
      setRows(prev => prev.filter(r => r.key !== row.key));
    } catch (err) {
      console.error('Failed to delete agent config', err);
      useAppStore.getState().showToast(t('agentConfigDeleteFailed'), 'error');
    }
  };

  const handleRunDigest = async (configId: string) => {
    setRunningConfigId(configId);
    try {
      await apiClient.post('/agent-config/run', { config_id: configId });
      useAppStore.getState().showToast(t('digestRunAccepted'), 'info');
    } catch {
      useAppStore.getState().showToast(t('digestRunFailed'), 'error');
    } finally {
      setRunningConfigId(null);
    }
  };

  const handleKindChange = (key: string, kind: AgentKind) => {
    updateRow(key, {
      kind,
      titleTemplate: kind === 'digest' ? DIGEST_TITLE_TEMPLATE : EXTRACTOR_TITLE_TEMPLATE,
      subjectUserIds: null,
    });
  };

  const handleAddRow = () => {
    setRows(prev => [...prev, makeEmptyRow()]);
  };

  const handlePickerSelect = async (docId: string | null) => {
    if (!docId || pickerAnchor === null) return;
    const { rowKey, type } = pickerAnchor;
    if (type === 'config') {
      updateRow(rowKey, { selectedConfigDocId: docId });
    } else {
      updateRow(rowKey, { selectedTargetDocId: docId });
    }
    setPickerAnchor(null);
  };

  const getDocTitle = (docId: string | null) => {
    if (!docId) return '\u2014';
    const doc = documents.find(d => d.document_id === docId);
    return doc ? doc.title : docId;
  };

  if (loading) {
    return <div className="p-4 text-ui-base text-text-dim text-center">{t('loading')}</div>;
  }

  return (
    <div className="flex flex-col gap-2">
      {/* eslint-disable-next-line react/forbid-elements */}
      <button data-add-agent onClick={handleAddRow} className="hidden" />

      {rows.length === 0 && (
        <div className="text-ui-sm text-text-dim text-center py-2">
          {t('noAgentConfig')}
        </div>
      )}

      {rows.map(row => {
        const locked = row.kind === 'digest' && !canEditDigestBinding(isRoot, row.config, currentUserId);
        const incomplete = !row.selectedConfigDocId || !row.selectedTargetDocId;
        return (
        <div key={row.key} className="bg-surface2 border border-border-soft p-3">
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('agentKind')}</span>
              {/* WHY: kind is part of the binding's key — switching it on a saved row would
                  silently create a second binding instead of changing this one. */}
              <SegmentedControl
                ariaLabel={t('agentKind')}
                value={row.kind}
                disabled={!!row.config}
                options={[
                  { value: 'extractor', label: t('agentKindExtractor') },
                  { value: 'digest', label: t('agentKindDigest') },
                ]}
                onChange={v => handleKindChange(row.key, v as AgentKind)}
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('configDocument')}</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => setPickerAnchor({ rowKey: row.key, type: 'config', rect: e.currentTarget.getBoundingClientRect() })}
              >
                {row.selectedConfigDocId ? getDocTitle(row.selectedConfigDocId) : t('selectConfigDoc')}
              </Button>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('targetDocument')}</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => setPickerAnchor({ rowKey: row.key, type: 'target', rect: e.currentTarget.getBoundingClientRect() })}
              >
                {row.selectedTargetDocId ? getDocTitle(row.selectedTargetDocId) : t('selectTargetDoc')}
              </Button>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('triggerEvent')}</span>
              <div onClick={e => e.stopPropagation()}>
                <Dropdown
                  align="right"
                  placement="bottom"
                  value={row.triggerEvent}
                  options={TRIGGER_EVENTS}
                  onSelect={v => updateRow(row.key, { triggerEvent: v })}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1" onClick={e => e.stopPropagation()}>
              <span className="text-ui-sm text-text-dim">{t('titleTemplate')}</span>
              <FieldInput
                value={row.titleTemplate}
                onChange={e => updateRow(row.key, { titleTemplate: e.target.value })}
                placeholder={row.kind === 'digest' ? t('digestTitleTemplatePlaceholder') : t('titleTemplatePlaceholder')}
              />
              <span className="text-ui-2xs text-text-muted">{t('titleTemplateHint')}</span>
            </div>
            {row.kind === 'digest' && canPickDigestSubjects(isRoot, row.config, currentUserId) && members.length > 0 && (
              <DigestSubjectPicker
                members={members}
                selected={row.subjectUserIds ?? (currentUserId ? [currentUserId] : [])}
                onChange={ids => updateRow(row.key, { subjectUserIds: ids })}
              />
            )}
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('modelOverride')}</span>
              {toolModels.length > 0 ? (
                <div onClick={e => e.stopPropagation()}>
                  <Dropdown
                    align="right"
                    placement="bottom"
                    value={row.modelOverride}
                    options={[
                      { value: '', label: t('modelOverridePlaceholder') },
                      ...extractorModelOptions(
                        toolModels, structuredOutputModels, row.modelOverride, t('modelOverrideUnsupported'),
                      ),
                    ]}
                    onSelect={v => updateRow(row.key, { modelOverride: v })}
                  />
                </div>
              ) : (
                <FieldInput
                  value={row.modelOverride}
                  onChange={e => updateRow(row.key, { modelOverride: e.target.value })}
                  placeholder={t('modelOverridePlaceholder')}
                  className="max-w-[180px]"
                />
              )}
            </div>
          </div>

          {locked && (
            <div className="mt-3 text-ui-2xs text-text-muted">{t('digestLockedHint')}</div>
          )}
          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-border-soft">
            <AgentDeleteButton onDelete={() => handleDelete(row)} />
            <span className="text-ui-2xs text-text-muted">
              {row.saving ? t('saving') : incomplete ? t('pipelineDraftHint') : row.config && row.rev === row.savedRev ? t('saved') : ''}
            </span>
            <div className="flex-1" />
            {row.config && row.kind === 'digest' && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => row.config && handleRunDigest(row.config.config_id)}
                disabled={locked || runningConfigId === row.config.config_id}
              >
                <Play size={12} />
                {t('digestRunNow')}
              </Button>
            )}
          </div>
        </div>
        );
      })}

      {pickerAnchor && (
        <ParentPickerPopup
          currentDocumentId={
            pickerAnchor.type === 'config'
              ? rows.find(r => r.key === pickerAnchor.rowKey)?.selectedConfigDocId
              : rows.find(r => r.key === pickerAnchor.rowKey)?.selectedTargetDocId
          }
          anchorRect={pickerAnchor.rect}
          onClose={() => setPickerAnchor(null)}
          onMoved={handlePickerSelect}
        />
      )}
    </div>
  );
}
