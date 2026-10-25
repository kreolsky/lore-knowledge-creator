/** Decision card for a still-held mutating call (mid-turn approval).
 *
 * Rendered from `message.pending_verdicts` (the `lore/verdict-ask` mint while
 * live; GET /chat/verdicts after reload). Three actions — allow once, allow the
 * tool for the session, reject with text. Publishing the verdict resolves the
 * held call; the resolved call's tool result replaces the card.
 *
 * `argsRaw` (optional): the held call's own arguments, read off its tool-call
 * node by the caller — the ask frame itself carries only {turn, callId,
 * toolName}, and dsh appends the call node BEFORE asking. The reload path
 * (GET /chat/verdicts, no nodes) passes nothing and keeps the bare card.
 */

import { useState } from 'react';
import { ShieldAlert } from 'lucide-react';
import { Button, FieldInput } from '../ui';
import { ToolPlate } from './ToolPlate';
import type { PendingVerdict } from '../../types';
import { useChatStore } from '../../store/chat-store';
import { useTranslation } from '../../i18n';

/** One diff half. Clamped (max-h + scroll) so a huge string cannot blow up
 * the chat column; red = removed, green = added (the plate body caps the
 * whole card, this cap keeps ONE edit from eating that budget). */
function DiffPre({ text, tone }: { text: string; tone: 'removed' | 'added' }) {
  return (
    <pre className={`text-xs font-mono whitespace-pre-wrap break-words m-0 max-h-40 overflow-y-auto ${tone === 'removed' ? 'text-red' : 'text-green'}`}>
      {text}
    </pre>
  );
}

const RAW_PRE_CLASS = 'text-xs font-mono whitespace-pre-wrap break-words m-0 max-h-40 overflow-y-auto';

/** A plain object entry read as a string ('' when not a string) — the args are
 * the model's own JSON, never trusted past rendering. */
function strArg(rec: Record<string, unknown>, key: string): string {
  const v = rec[key];
  return typeof v === 'string' ? v : '';
}

/** Object items of an `edits[]` array; a non-object item (bad model JSON) is
 * dropped rather than crashing the card. */
function editsArg(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter((e): e is Record<string, unknown> => typeof e === 'object' && e !== null);
}

/** The held call's proposed change, mapped per tool: the two pointwise edit
 * tools show removed/added pairs (edit_table_cell labelled by its cell), any
 * other tool shows pretty-printed JSON, unparsable args show the raw string.
 * Preview only — the verdict buttons stay the decision. */
function VerdictArgs({ toolName, argsRaw }: { toolName: string; argsRaw: string }) {
  let parsed: Record<string, unknown> | null = null;
  try {
    const v: unknown = JSON.parse(argsRaw);
    if (typeof v === 'object' && v !== null && !Array.isArray(v)) parsed = v as Record<string, unknown>;
  } catch { /* unparsable → raw string below */ }
  if (parsed === null) {
    return <pre className={RAW_PRE_CLASS}>{argsRaw}</pre>;
  }
  if (toolName === 'edit_document') {
    const edits = editsArg(parsed.edits);
    if (edits.length > 0) {
      return (
        <div className="space-y-2">
          {edits.map((e, i) => (
            <div key={i} className="space-y-1">
              <DiffPre text={strArg(e, 'old_string')} tone="removed" />
              <DiffPre text={strArg(e, 'new_string')} tone="added" />
            </div>
          ))}
        </div>
      );
    }
  }
  if (toolName === 'edit_table_cell') {
    const edits = editsArg(parsed.edits);
    if (edits.length > 0) {
      return (
        <div className="space-y-2">
          {edits.map((e, i) => (
            <div key={i} className="space-y-1">
              <div className="text-ui-sm text-text-dim">{strArg(e, 'column')} · {String(e.row ?? '')}</div>
              <DiffPre text={strArg(e, 'old_value')} tone="removed" />
              <DiffPre text={strArg(e, 'new_value')} tone="added" />
            </div>
          ))}
        </div>
      );
    }
  }
  return <pre className={RAW_PRE_CLASS}>{JSON.stringify(parsed, null, 2)}</pre>;
}

export function VerdictCard({ pending, argsRaw }: { pending: PendingVerdict; argsRaw?: string }) {
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
      <div className="space-y-2">
        {argsRaw ? <VerdictArgs toolName={pending.tool_name} argsRaw={argsRaw} /> : null}
        <div className="text-ui-sm text-text-dim">{t('verdictBody')}</div>
      </div>
    </ToolPlate>
  );
}
