/** Admin embeddings tab — doc + chat-index coverage bullets, embed-now, index-chats, rebuild-index. */

import { useEffect, useState, useCallback, useRef } from 'react';
import { Button, CenterHeading, FieldInput } from '../ui';
import { apiClient } from '../../api/client';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';

interface EmbeddingStats {
  doc_chunks_count: number;
  docs_without_embeddings: number;
  refs_without_embeddings: number;
  pending_reembed_count: number;
  stale_model_count: number;
  chat: ChatIndexStats;
}

type ChatIndexState = 'on' | 'skill_disabled' | 'embeddings_unconfigured';

/** pending/stale are null while indexing is off — never 0, which would read as "all indexed". */
interface ChatIndexStats {
  state: ChatIndexState;
  chunks: number;
  pending_messages: number | null;
  stale_chunks: number | null;
}

const CHAT_OFF_KEY = {
  skill_disabled: 'chatIndexOffSkill',
  embeddings_unconfigured: 'chatIndexOffEmbeddings',
} as const;

interface EmbedStatus {
  running: boolean;
  operation: string;
  processed: number;
  total: number;
  errors: number;
}

function StatItem({ label, value, alert }: { label: string; value: number; alert?: boolean }) {
  return (
    <li>
      {label}: <span className={`tabular-nums ${alert ? 'text-red font-semibold' : 'text-text-dim'}`}>{value}</span>
    </li>
  );
}

export function EmbeddingsTab() {
  const [stats, setStats] = useState<EmbeddingStats | null>(null);
  const [status, setStatus] = useState<EmbedStatus>({ running: false, operation: 'embedding', processed: 0, total: 0, errors: 0 });
  const [confirmRebuild, setConfirmRebuild] = useState(false);
  const [confirmInput, setConfirmInput] = useState('');
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);

  const loadStats = useCallback(async () => {
    try {
      const data = await apiClient.get('/admin/embeddings/stats');
      setStats(data);
    } catch {
      showToast(t('failedToLoadEmbeddingStats'), 'error');
    }
  }, [showToast]);

  const failCountRef = useRef(0);
  const loadStatus = useCallback(async () => {
    try {
      const data = await apiClient.get('/admin/embeddings/status');
      setStatus(data);
      failCountRef.current = 0;
    } catch {
      failCountRef.current++;
      if (failCountRef.current >= 3) {
        showToast(t('failedToLoadEmbeddingStatus'), 'error');
        failCountRef.current = 0;
      }
    }
  }, [showToast]);

  useEffect(() => {
    loadStats();
    loadStatus();
  }, [loadStats, loadStatus]);

  useEffect(() => {
    if (!status.running) return;
    const interval = setInterval(loadStatus, 2000);
    return () => clearInterval(interval);
  }, [status.running, loadStatus]);

  const wasRunning = useRef(false);

  useEffect(() => {
    if (wasRunning.current && !status.running) {
      loadStats();
    }
    wasRunning.current = status.running;
  }, [status.running, loadStats]);

  const handleEmbedMissing = async () => {
    try {
      const data = await apiClient.post('/admin/embeddings/embed-missing', {});
      if (data.started === false && data.error) {
        showToast(data.error, 'error');
        return;
      }
      showToast(t('embedEnqueued', { count: data.count }), 'info');
      loadStatus();
    } catch {
      showToast(t('failedToStartEmbedding'), 'error');
    }
  };

  const handleIndexChats = async () => {
    try {
      const data = await apiClient.post('/admin/embeddings/index-chats', {});
      if (data.started === false) {
        // WHY: the backend refuses with the index state token, not prose.
        const offKey = CHAT_OFF_KEY[data.error as keyof typeof CHAT_OFF_KEY];
        showToast(t(offKey ?? 'failedToStartChatIndex'), 'error');
        return;
      }
      showToast(t('chatIndexEnqueued', { count: data.count }), 'info');
      loadStats();
    } catch {
      showToast(t('failedToStartChatIndex'), 'error');
    }
  };

  const cancelRebuild = () => {
    setConfirmRebuild(false);
    setConfirmInput('');
  };

  const handleRebuild = async () => {
    cancelRebuild();
    try {
      // WHY: a concurrent reset answers 200 with {success:false, error}, not 4xx —
      // the catch below never sees it, so the body is read explicitly.
      const data = await apiClient.post('/admin/embeddings/reset-docs', {});
      if (data.success === false && data.error) {
        showToast(data.error, 'error');
        return;
      }
      loadStatus();
    } catch {
      showToast(t('failedToRebuildIndex'), 'error');
    }
  };

  const missing = stats
    ? stats.docs_without_embeddings + stats.refs_without_embeddings + stats.pending_reembed_count + stats.stale_model_count
    : 0;
  const chat = stats?.chat;
  const chatWork = chat?.state === 'on' ? (chat.pending_messages ?? 0) + (chat.stale_chunks ?? 0) : 0;

  return (
    <div className="space-y-6">
      <CenterHeading>{t('embeddings')}</CenterHeading>

      {stats && (
        <ul className="list-disc pl-5 space-y-1 text-sm text-text">
          <StatItem label={t('chunksInIndex')} value={stats.doc_chunks_count} />
          <StatItem label={t('docsWithoutEmbeddings')} value={stats.docs_without_embeddings} />
          <StatItem label={t('refsWithoutEmbeddings')} value={stats.refs_without_embeddings} />
          <StatItem label={t('embeddingFailed')} value={stats.pending_reembed_count} alert={stats.pending_reembed_count > 0} />
          <StatItem label={t('embeddingStaleModel')} value={stats.stale_model_count} alert={stats.stale_model_count > 0} />
        </ul>
      )}

      {chat && (
        <div className="space-y-1">
          <div className="text-xs text-text-dim">{t('chatIndex')}</div>
          <ul className="list-disc pl-5 space-y-1 text-sm text-text">
            <StatItem label={t('chatChunksInIndex')} value={chat.chunks} />
            {chat.state === 'on' ? (
              <>
                <StatItem label={t('chatPendingMessages')} value={chat.pending_messages ?? 0} alert={(chat.pending_messages ?? 0) > 0} />
                <StatItem label={t('chatStaleChunks')} value={chat.stale_chunks ?? 0} alert={(chat.stale_chunks ?? 0) > 0} />
              </>
            ) : (
              <li className="text-text-dim" data-chat-index-off>{t(CHAT_OFF_KEY[chat.state])}</li>
            )}
          </ul>
        </div>
      )}

      {status.running && (
        <div>
          <div className="text-xs text-text-dim mb-2">
            {status.operation === 'reset_docs' ? t('rebuildingIndex') : t('embeddingProgress')}
            : {status.processed}/{status.total}
            {status.errors > 0 && ` (${status.errors} ${t('errors')})`}
          </div>
          <div className="w-full bg-surface2 h-2">
            <div
              className="bg-accent h-2"
              style={{ width: `${status.total > 0 ? (status.processed / status.total) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}

      {/* One grid = ONE shared button column (equal widths, styling.md lesson);
          the hint spans take the rest of the row. */}
      <div className="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-3">
        <Button variant="primary" size="lg" onClick={handleEmbedMissing} disabled={status.running || missing === 0}>
          {status.running ? t('embeddingRunning') : t('embedNow')}
        </Button>
        <span className="text-xs text-text-dim">{t('embedNowHint')}</span>

        <Button size="lg" onClick={handleIndexChats} disabled={chatWork === 0}>
          {t('indexChatsNow')}
        </Button>
        <span className="text-xs text-text-dim">{t('indexChatsNowHint')}</span>

        {!confirmRebuild ? (
          <>
            <Button variant="danger" size="lg" onClick={() => setConfirmRebuild(true)} disabled={status.running}>
              {t('rebuildIndex')}
            </Button>
            <span className="text-xs text-text-dim">{t('rebuildIndexHint')}</span>
          </>
        ) : (
          <div className="col-span-2 space-y-2">
            <div className="text-xs text-text-dim">{t('typeRebuildConfirm')}</div>
            <FieldInput
              className="w-full"
              value={confirmInput}
              onChange={e => setConfirmInput(e.target.value)}
              placeholder={t('rebuildConfirmWord')}
            />
            <div className="flex gap-2">
              <Button
                variant="danger"
                size="sm"
                onClick={handleRebuild}
                disabled={confirmInput !== t('rebuildConfirmWord')}
              >
                {t('confirm')}
              </Button>
              <Button size="sm" onClick={cancelRebuild}>
                {t('cancel')}
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
