/**
 * Transcription agent config manager — sub-panel in ReferencesPanel.
 * Self-contained with local state only — no store involvement.
 * Supports multiple agent configs per document, each with a trigger event.
 */

import { useState, useEffect, useCallback } from 'react';
import { Trash2 } from 'lucide-react';
import { apiClient } from '../../api/client';
import { AgentConfig } from '../../types';
import { useAppStore } from '../../store/app-store';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';
import { Button, FieldInput, Dropdown } from '../ui';
import { ParentPickerPopup } from '../ParentPickerPopup';
import { useArmedAction } from '../../hooks/useArmedAction';

const TRIGGER_EVENTS = [
  { value: 'transcription_complete', label: 'transcription_complete' },
];

interface Props {
  documentId: string;
}

interface ConfigRow {
  config: AgentConfig | null;
  selectedConfigDocId: string | null;
  selectedTargetDocId: string | null;
  triggerEvent: string;
  titleTemplate: string;
  modelOverride: string;
  saving: boolean;
}

function makeEmptyRow(): ConfigRow {
  return {
    config: null,
    selectedConfigDocId: null,
    selectedTargetDocId: null,
    triggerEvent: 'transcription_complete',
    titleTemplate: 'extractor-{yyyy}-{mm}-{dd} {HH}:{MM} {rnd:4}',
    modelOverride: '',
    saving: false,
  };
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
  const models = useChatStore(s => s.models);
  const modelsLoaded = useChatStore(s => s.modelsLoaded);
  const loadModels = useChatStore(s => s.loadModels);
  const [rows, setRows] = useState<ConfigRow[]>([makeEmptyRow()]);
  const [loading, setLoading] = useState(true);
  const [pickerAnchor, setPickerAnchor] = useState<{ rowIdx: number; type: 'config' | 'target'; rect: DOMRect } | null>(null);

  useEffect(() => {
    if (!modelsLoaded) loadModels();
  }, [modelsLoaded, loadModels]);

  const loadConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiClient.get(`/agent-configs?document_id=${documentId}`);
      if (data && Array.isArray(data) && data.length > 0) {
        const configRows: ConfigRow[] = data.map((c: AgentConfig) => ({
          config: c,
          selectedConfigDocId: c.config_doc_id,
          selectedTargetDocId: c.target_doc_id,
          triggerEvent: c.trigger_event || 'transcription_complete',
          titleTemplate: c.title_template || 'extractor-{yyyy}-{mm}-{dd} {HH}:{MM} {rnd:4}',
          modelOverride: c.model || '',
          saving: false,
        }));
        setRows(configRows);
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

  const updateRow = (idx: number, patch: Partial<ConfigRow>) => {
    setRows(prev => prev.map((r, i) => i === idx ? { ...r, ...patch } : r));
  };

  const handleSave = async (idx: number) => {
    const row = rows[idx];
    if (!row.selectedConfigDocId || !row.selectedTargetDocId) return;
    updateRow(idx, { saving: true });
    try {
      await apiClient.put('/agent-config', {
        document_id: documentId,
        config_doc_id: row.selectedConfigDocId,
        target_doc_id: row.selectedTargetDocId,
        trigger_event: row.triggerEvent,
        title_template: row.titleTemplate || null,
        model: row.modelOverride || null,
      });
      await loadConfigs();
    } catch (err) {
      console.error('Failed to save agent config', err);
      useAppStore.getState().showToast(t('agentConfigSaveFailed'), 'error');
      updateRow(idx, { saving: false });
    }
  };

  const handleDelete = async (idx: number) => {
    const row = rows[idx];
    if (!row.config) return;
    try {
      await apiClient.delete(`/agent-config/${row.config.config_id}`);
      await loadConfigs();
    } catch (err) {
      console.error('Failed to delete agent config', err);
      useAppStore.getState().showToast(t('agentConfigDeleteFailed'), 'error');
    }
  };

  const handleAddRow = () => {
    setRows(prev => [...prev, makeEmptyRow()]);
  };

  const handlePickerSelect = async (docId: string | null) => {
    if (!docId || pickerAnchor === null) return;
    const { rowIdx, type } = pickerAnchor;
    if (type === 'config') {
      updateRow(rowIdx, { selectedConfigDocId: docId });
    } else {
      updateRow(rowIdx, { selectedTargetDocId: docId });
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

      {rows.map((row, idx) => (
        <div key={idx} className="bg-surface2 border border-border-soft p-3">
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('configDocument')}</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => setPickerAnchor({ rowIdx: idx, type: 'config', rect: e.currentTarget.getBoundingClientRect() })}
              >
                {row.selectedConfigDocId ? getDocTitle(row.selectedConfigDocId) : t('selectConfigDoc')}
              </Button>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('targetDocument')}</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => setPickerAnchor({ rowIdx: idx, type: 'target', rect: e.currentTarget.getBoundingClientRect() })}
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
                  onSelect={v => updateRow(idx, { triggerEvent: v })}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1" onClick={e => e.stopPropagation()}>
              <span className="text-ui-sm text-text-dim">{t('titleTemplate')}</span>
              <FieldInput
                value={row.titleTemplate}
                onChange={e => updateRow(idx, { titleTemplate: e.target.value })}
                placeholder={t('titleTemplatePlaceholder')}
              />
              <span className="text-ui-2xs text-text-muted">{t('titleTemplateHint')}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ui-sm text-text-dim">{t('modelOverride')}</span>
              {models.length > 0 ? (
                <div onClick={e => e.stopPropagation()}>
                  <Dropdown
                    align="right"
                    placement="bottom"
                    value={row.modelOverride}
                    options={[
                      { value: '', label: t('modelOverridePlaceholder') },
                      ...models.map(m => ({ value: m, label: m })),
                    ]}
                    onSelect={v => updateRow(idx, { modelOverride: v })}
                  />
                </div>
              ) : (
                <FieldInput
                  value={row.modelOverride}
                  onChange={e => updateRow(idx, { modelOverride: e.target.value })}
                  placeholder={t('modelOverridePlaceholder')}
                  className="max-w-[180px]"
                />
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 mt-3 pt-3 border-t border-border-soft">
            {row.config && <AgentDeleteButton onDelete={() => handleDelete(idx)} />}
            <div className="flex-1" />
            <Button
              variant="primary"
              size="sm"
              onClick={() => handleSave(idx)}
              disabled={!row.selectedConfigDocId || !row.selectedTargetDocId || row.saving}
            >
              {row.saving ? t('saving') : t('save')}
            </Button>
          </div>
        </div>
      ))}

      {pickerAnchor && (
        <ParentPickerPopup
          currentDocumentId={
            pickerAnchor.type === 'config'
              ? rows[pickerAnchor.rowIdx]?.selectedConfigDocId
              : rows[pickerAnchor.rowIdx]?.selectedTargetDocId
          }
          anchorRect={pickerAnchor.rect}
          onClose={() => setPickerAnchor(null)}
          onMoved={handlePickerSelect}
        />
      )}
    </div>
  );
}
