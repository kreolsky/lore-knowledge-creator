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
import { ShieldAlert, AlertTriangle } from 'lucide-react';
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

/** Tools asked on every call. Display only: the driver refuses the session
 * grant by the backend's per_call_approval flag, whatever the card offers. */
const NO_SESSION_GRANT_TOOLS = new Set(['http_request']);

const RAW_PRE_CLASS = 'text-xs font-mono whitespace-pre-wrap break-words m-0 max-h-40 overflow-y-auto';

/** A plain object entry read as a string ('' when not a string) — the args are
 * the model's own JSON, never trusted past rendering. */
function strArg(rec: Record<string, unknown>, key: string): string {
  const v = rec[key];
  return typeof v === 'string' ? v : '';
}

/** Object items of an array arg (`edits[]`, `verdicts[]`); a non-object item
 * (bad model JSON) is dropped rather than crashing the card. */
function objectItems(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter((e): e is Record<string, unknown> => typeof e === 'object' && e !== null);
}

/** The held call's proposed change, mapped per tool: the two pointwise edit
 * tools show removed/added pairs (edit_table_cell labelled by its cell), the
 * two memory-capture tools show their prose (facts to save, the anchor
 * target), any other tool shows pretty-printed JSON, unparsable args show
 * the raw string. Preview only — the verdict buttons stay the decision. */
function VerdictArgs({ toolName, argsRaw }: { toolName: string; argsRaw: string }) {
  const { t } = useTranslation();
  let parsed: Record<string, unknown> | null = null;
  try {
    const v: unknown = JSON.parse(argsRaw);
    if (typeof v === 'object' && v !== null && !Array.isArray(v)) parsed = v as Record<string, unknown>;
  } catch { /* unparsable → raw string below */ }
  if (parsed === null) {
    return <pre className={RAW_PRE_CLASS}>{argsRaw}</pre>;
  }
  if (toolName === 'edit_document') {
    const edits = objectItems(parsed.edits);
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
    const edits = objectItems(parsed.edits);
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
  if (toolName === 'save_chat_facts') {
    // One batch, one card: each verdict is one fact the agent proposes —
    // title + body as prose (the moderation the reject-with-text performs),
    // never the raw JSON envelope. `merge`/`supersede` act on a STORED fact,
    // so their line says so (a `new` needs no label; a `skip` shows its
    // reason as the body, the only content it carries).
    const verdicts = objectItems(parsed.verdicts);
    if (verdicts.length > 0) {
      return (
        <div className="space-y-2">
          {verdicts.map((v, i) => {
            const action = strArg(v, 'action');
            const body = strArg(v, 'text') || strArg(v, 'reason');
            return (
              <div key={i} className="space-y-1">
                {strArg(v, 'title') !== '' && (
                  <div className="text-ui-sm font-medium text-text">{strArg(v, 'title')}</div>
                )}
                {body !== '' && (
                  <div className="text-ui-sm text-text-dim whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                    {body}
                  </div>
                )}
                {action === 'merge' && <div className="text-xs text-text-dim">{t('verdictFactMerge')}</div>}
                {action === 'supersede' && <div className="text-xs text-text-dim">{t('verdictFactSupersede')}</div>}
              </div>
            );
          })}
        </div>
      );
    }
  }
  if (toolName === 'set_fact_anchor') {
    const docs = Array.isArray(parsed.document_ids)
      ? parsed.document_ids.filter((d): d is string => typeof d === 'string')
      : [];
    return (
      <div className="space-y-1">
        <div className="text-ui-sm text-text-dim">
          {t('verdictAnchorFact')}: <span className="font-mono break-all">{strArg(parsed, 'fact_id')}</span>
        </div>
        {docs.length > 0 ? (
          <div className="text-ui-sm text-text-dim">
            {t('verdictAnchorDocs')}: <span className="font-mono break-all">{docs.join(', ')}</span>
          </div>
        ) : (
          <div className="text-ui-sm text-text-dim">{t('verdictAnchorClear')}</div>
        )}
      </div>
    );
  }
  return <pre className={RAW_PRE_CLASS}>{JSON.stringify(parsed, null, 2)}</pre>;
}

export function VerdictCard({ pending, argsRaw }: { pending: PendingVerdict; argsRaw?: string }) {
  const { t } = useTranslation();
  const decideVerdict = useChatStore(s => s.decideVerdict);
  // The ACTIVE chat's parent, for the no-parent warning on save_chat_facts:
  // the facts follow that parent in a document narrow, so a chat without one
  // parks them project-wide only (the plan's accepted-risk warning line).
  const activeParentDocId = useChatStore(s => {
    const active = s.activeSessionId;
    const row = active ? s.sessions.find(x => x.session_id === active) : undefined;
    return row?.document_id ?? null;
  });
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
                className="min-w-0 flex-1"
              />
              <Button
                variant="danger"
                onClick={() => void decide('reject')}
                disabled={busy !== null}
              >
                {t('verdictReject')}
              </Button>
              <Button variant="ghost" onClick={() => setRejecting(false)} disabled={busy !== null}>
                {t('verdictCancel')}
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="primary"
                onClick={() => void decide('allow_once')}
                disabled={busy !== null}
              >
                {t('verdictAllowOnce')}
              </Button>
              {!NO_SESSION_GRANT_TOOLS.has(pending.tool_name) && (
                <Button
                  variant="ghost"
                  onClick={() => void decide('allow_session')}
                  disabled={busy !== null}
                >
                  {t('verdictAllowSession')}
                </Button>
              )}
              <Button
                variant="danger"
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
        {pending.tool_name === 'save_chat_facts' && activeParentDocId === null && (
          <div className="flex items-start gap-1.5 text-xs text-yellow-500">
            <AlertTriangle size={13} className="mt-px shrink-0" />
            <span>{t('verdictSaveNoParent')}</span>
          </div>
        )}
        <div className="text-ui-sm text-text-dim">{t('verdictBody')}</div>
      </div>
    </ToolPlate>
  );
}
