/**
 * Visible overlay that blocks the editor surface when collab is not connected.
 *
 * Renders three visual states: connecting (initial), reconnecting (with attempt
 * counter), offline (with retry button). Sits above the CM6 area while
 * sidebar/header remain interactive.
 */
// ARCH: presentation-only — connection state is owned by Editor.tsx; the
// overlay is rendered as a sibling above CM6 inside a relative container.
// SYSTEM: editor-offline-overlay — blocks input outside collab status==='connected'

import { useTranslation } from '../../i18n';
import { Button } from '../ui';
import type { WsStatus } from '../../collab/ws-status';

interface Props {
  status: WsStatus;
  collabReady: boolean;
  reconnectAttempts: number;
  maxAttempts: number;
  onRetry: () => void;
}

export function EditorOfflineOverlay({
  status, collabReady, reconnectAttempts, maxAttempts, onRetry,
}: Props) {
  const { t } = useTranslation();

  const visible = status !== 'connected' || !collabReady;
  if (!visible) return null;

  const isOffline = status === 'offline';
  const isReconnecting = status === 'reconnecting';

  let title: string;
  let sub: string | null;
  if (isOffline) {
    title = t('collabOverlayOfflineTitle');
    sub = t('collabOverlayOfflineSub');
  } else if (isReconnecting) {
    title = t('collabOverlayReconnectingTitle');
    sub = t('collabOverlayReconnectingSub', { attempt: reconnectAttempts, max: maxAttempts });
  } else {
    // 'connecting' OR ('connected' && !collabReady) — both are "waiting for init"
    title = t('collabOverlayConnectingTitle');
    sub = null;
  }

  return (
    <div
      data-testid="editor-offline-overlay"
      className="absolute inset-0 z-30 flex items-center justify-center bg-bg/90 backdrop-blur-sm pointer-events-auto"
    >
      <div className="flex flex-col items-center gap-3 max-w-md px-6 text-center">
        {!isOffline && (
          <div
            data-testid="overlay-spinner"
            className="w-6 h-6 border-2 border-text-dim border-t-transparent animate-spin"
          />
        )}
        <div className="text-base font-medium text-text">{title}</div>
        {sub && <div className="text-sm text-text-dim">{sub}</div>}
        <div className="text-xs text-text-dim mt-2">{t('collabOverlayMicrocopy')}</div>
        {isOffline && (
          <Button variant="primary" size="sm" onClick={onRetry}>
            {t('collabOverlayRetry')}
          </Button>
        )}
      </div>
    </div>
  );
}
