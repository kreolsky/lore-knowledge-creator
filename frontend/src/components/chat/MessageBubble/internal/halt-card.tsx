/** Agent-halt card, split out of MessageBubble.tsx (behavior-preserving). */

import { AlertTriangle } from 'lucide-react';
import { Button } from '../../../ui';
import { useTranslation, type TranslationKey } from '../../../../i18n';
import type { HaltReason } from '../../../../types';

// The agent-halt card. reason → i18n label; a derived recap
// line; a Continue button (a visible user message). A halt is a result, not a
// failure, so it uses a neutral info surface (NOT the red error styling).
export function HaltCard({ reason, steps, limit, toolCallCount, onContinue }: {
  reason: HaltReason;
  steps?: number;
  limit?: number;
  toolCallCount: number;
  onContinue?: () => void;
}) {
  const { t } = useTranslation();
  // INVARIANT: every reason that reaches this card has a label, known or not.
  // Why: the card draws TWO vocabularies — the live budget halts of `HaltReason`
  // and the backend's wider abnormal set written to `messages.halt` (error,
  // turn_timeout, line_unreachable, stream_failed) — and a reason with no arm
  // used to reach `t()` as undefined and crash the whole chat.
  const reasonLabel = ({
    tool_call_limit: 'chatHaltReasonToolCallLimit',
    repeat_limit: 'chatHaltReasonRepeatLimit',
    step_limit: 'chatHaltReasonStepLimit',
    output_token_limit: 'chatHaltReasonOutputTokenLimit',
    context_limit: 'chatHaltReasonContextLimit',
    disconnected: 'chatHaltReasonDisconnected',
    error: 'chatHaltReasonError',
    turn_timeout: 'chatHaltReasonTurnTimeout',
    line_unreachable: 'chatHaltReasonLineUnreachable',
    stream_failed: 'chatHaltReasonStreamFailed',
  } as Record<string, TranslationKey>)[reason];
  return (
    <div className="my-1 flex items-start gap-2 border border-border-soft bg-surface2 px-3 py-2 text-sm">
      <AlertTriangle size={15} className="mt-0.5 shrink-0 text-text-dim" />
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="text-text-main">
          {reasonLabel ? t(reasonLabel) : t('chatHaltReasonUnknown', { reason: String(reason) })}
        </div>
        {/* Recap is DERIVED from the turn's counted tool calls — no LLM call.
            The chips above already show the detail; this is a one-line summary of
            what happened. context_limit omits it (the gauge owns the counters). */}
        {reason !== 'context_limit' && toolCallCount > 0 && (
          <div className="text-text-dim text-xs">
            {t('chatHaltRecapTools', { n: toolCallCount })}
          </div>
        )}
        {reason === 'step_limit' && steps != null && limit != null && (
          <div className="text-text-dim text-xs">
            {t('chatHaltStepCount', { steps, limit })}
          </div>
        )}
        {reason === 'disconnected' && steps != null && (
          <div className="text-text-dim text-xs">
            {t('chatHaltStepCountNoLimit', { steps })}
          </div>
        )}
        {onContinue && reason !== 'disconnected' && (
          <div className="pt-1">
            {/* INVARIANT: Continue carries the SAME accent as the composer's Send
                (variant="primary"), never a muted one.
                Why: a halt leaves the turn unfinished and this button is the only way
                to resume it, so it has to read as the primary action — as `subtle` it
                sat on bg-surface2 inside an already bg-surface2 card and was
                effectively invisible against it. `disconnected` is excluded: the frame
                consumer is gone, so resume is a fresh consolidate_memory call (which
                auto-resumes off the reference stamp), not a Continue on this card. */}
            <Button variant="primary" size="sm" onClick={onContinue}>
              {t('chatHaltContinue')}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
