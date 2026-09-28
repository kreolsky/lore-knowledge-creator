/** Decision card for a still-held mutating call (mid-turn approval).

 * Rendered from `message.pending_verdicts` (the `lore/verdict-ask` mint while
 * live; GET /chat/verdicts after reload). Three actions — allow once, allow the
 * tool for the session, reject with text. Publishing the verdict resolves the
 * held call; the resolved call's tool result replaces the card.
 */

import { useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { Button, FieldInput } from '../ui';
import { ToolPlate } from './ToolPlate';
import type { PendingVerdict } from '../../types';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';

export function VerdictCard({ pending }: { pending: PendingVerdict }) {
  const { t } = useTranslation();
  const decideVerdict = useChatStore(s => s.decideVerdict);
  const [busy, setBusy] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');

  const decide = async (action: 'allow_once' | 'allow_session' | 'reject') => {
    setBusy(action);
    try {
      await decideVerdict(
        pending.call_id,
        action,
        pending.tool_name,
        action === 'reject' ? reason : undefined,
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <ToolPlate
      icon={<ShieldAlert size={13} />}
      title={pending.tool_name}
      defaultExpanded
      footer={
        <div className="flex items-center justify-end gap-1 text-ui-base">
          {rejecting ? (
            <>
              <FieldInput
                value={reason}
                onChange={e => setReason(e.target.value)}
                placeholder={t('verdictReasonPlaceholder')}
                className="h-7 min-w-0 flex-1"
              />
              <Button
                variant="danger" size="sm"
                onClick={() => void decide('reject')}
                disabled={busy !== null}
              >
                {t('verdictReject')}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setRejecting(false)} disabled={busy !== null}>
                {t('verdictCancel')}
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="primary" size="sm"
                onClick={() => void decide('allow_once')}
                disabled={busy !== null}
              >
                {t('verdictAllowOnce')}
              </Button>
              <Button
                variant="ghost" size="sm"
                onClick={() => void decide('allow_session')}
                disabled={busy !== null}
              >
                {t('verdictAllowSession')}
              </Button>
              <Button
                variant="danger" size="sm"
                onClick={() => setRejecting(true)}
                disabled={busy !== null}
              >
                {t('verdictReject')}
              </Button>
            </>
          )}
        </div>
      }
    >
      <div className="text-ui-sm text-text-dim">{t('verdictBody')}</div>
    </ToolPlate>
  );
}
