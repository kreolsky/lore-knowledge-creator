/**
 * The assembled turn's renderer — Lore's components over the dsh nodes' data.
 *
 * // SYSTEM: dsh-conversation. The ONE rule (plan lore-renders-dsh-conversation,
 * //   Decisions): these components READ node data and never recompute it — no
 * //   second fold, no re-derived chip. Each `ChatNodeDataMap` kind maps to the
 * //   Lore component that draws it; `user`/`steering`/`context` draw nothing
 * //   here (Lore's own message rows render them), `turn-tail` draws nothing
 * //   (Lore's action row stays row-driven), and ANY other unrendered kind —
 * //   including `unknown` — degrades to the neutral fallback chip. A renderer
 * //   for a kind is an improvement, never the condition for being visible.
 */

import { useState } from 'react';
import { AlertTriangle, Brain, FileText, ShieldAlert } from 'lucide-react';
import { ToolPlate } from '../../ToolPlate';
import { MarkdownContent } from '../../MarkdownContent';
import { VerdictCard } from '../../VerdictCard';
import { HaltCard } from './halt-card';
import { AgentStepIcon, genPhaseLabel } from './agent-steps';
import { GeneratedImageThumb, ImageLightbox } from './image-lightbox';
import { useTranslation } from '../../../../i18n';
import { referenceFileUrl } from '../../../../utils/reference-url';
import type { HaltReason } from '../../../../types';

/** One published node, plain-data (the chat store's `conversation` entry). */
export interface TurnNodeLike {
  key: string;
  kind: string;
  anchorSeq: number;
  data: unknown;
}

interface AssistantBlockLike {
  kind: string;
  text?: string;
}

/** Read the text out of a settled tool result's content blocks (dsh core
 * ContentBlock[] — only the text blocks carry prose; others are dropped). */
function resultText(content: unknown): string {
  if (!Array.isArray(content)) return '';
  const parts: string[] = [];
  for (const block of content) {
    if (typeof block === 'object' && block !== null
      && (block as Record<string, unknown>).type === 'text') {
      parts.push(String((block as Record<string, unknown>).text ?? ''));
    }
  }
  return parts.join('\n');
}

/** The one tool chip: a running call renders as the running plate, a settled
 * one as the call's name + args gist with the result's text inside. */
function ToolCallNode({ data, isStreaming }: { data: unknown; isStreaming: boolean }) {
  const { t } = useTranslation();
  const root = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const block = root.root as Record<string, unknown> | undefined;
  if (!block) return null;
  const settled = block.kind === 'tool-result';
  const name = settled
    ? String((block.call as Record<string, unknown> | null)?.name ?? block.callId ?? 'tool')
    : String(block.name ?? 'tool');
  let gist = '';
  const argsRaw = settled
    ? String((block.call as Record<string, unknown> | null)?.argsRaw ?? '')
    : String(block.argsRaw ?? '');
  try {
    const parsed: unknown = JSON.parse(argsRaw);
    if (typeof parsed === 'object' && parsed !== null) {
      gist = Object.entries(parsed as Record<string, unknown>)
        .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
        .join(', ');
    }
  } catch { gist = argsRaw; }
  const isError = settled && block.isError === true;
  const body = settled ? resultText(block.content) : '';
  return (
    <ToolPlate
      icon={<AgentStepIcon outcome={isError ? 'failed' : undefined} />}
      title={gist
        ? <span className="flex items-center gap-1 min-w-0 w-full">
            <span className="shrink-0">{name}</span>
            <span className="opacity-50 shrink-0">·</span>
            <span className="truncate min-w-0 flex-1 opacity-70">{gist}</span>
          </span>
        : <span>{name}</span>}
      tone={isError ? 'failed' : undefined}
      ariaLabel={isError ? `${t('agentStepFailed')}: ${name}` : undefined}
      defaultExpanded={isStreaming && !settled}
      autoCollapse={settled}
      isStreaming={isStreaming && !settled}
    >
      {body
        ? <pre className="text-xs font-mono whitespace-pre-wrap break-words max-h-80 overflow-auto bg-surface2 border border-border-soft p-2 m-0">{body}</pre>
        : <div className="text-ui-sm text-text-dim">{t('agentStepNoop')}</div>}
    </ToolPlate>
  );
}

/** One assistant step: the text and reasoning blocks IN ORDER — the thinking
 * renders WHERE it happened (the plan's human-validation item). Tool-call
 * blocks draw nothing (the tool-call node is the chip). */
function AssistantStepNode({ data, isStreaming }: { data: unknown; isStreaming: boolean }) {
  const { t } = useTranslation();
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const blocks = Array.isArray(d.blocks) ? d.blocks as AssistantBlockLike[] : [];
  const hasText = blocks.some(b => b.kind === 'text' && (b.text ?? '').trim() !== '');
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === 'text' && (b.text ?? '').trim() !== '') {
          return <MarkdownContent key={i} content={b.text!} streaming={isStreaming} />;
        }
        if (b.kind === 'reasoning' && (b.text ?? '').trim() !== '') {
          return (
            <ToolPlate
              key={i}
              icon={<Brain size={13} />}
              title={t('reasoningLabel')}
              defaultExpanded={isStreaming && !hasText}
              autoCollapse
              collapseWhen={hasText}
              isStreaming={isStreaming}
            >
              <MarkdownContent content={b.text!} streaming={isStreaming} />
            </ToolPlate>
          );
        }
        return null;
      })}
    </>
  );
}

/** The verdict ask card. THE renderer call this step owed (plan Progress): the
 * card STAYS once asked — while the parked call has no tool row it renders the
 * decision card; once the call's own tool-call node exists (the verdict
 * published, the call ran) it renders as a settled record under that row. */
function VerdictAskNode({ data, nodes }: { data: unknown; nodes: TurnNodeLike[] }) {
  const { t } = useTranslation();
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const callId = String(d.callId ?? '');
  const toolName = String(d.toolName ?? '');
  // INVARIANT: the ask is decided only once the call's node carries a
  // tool-result — a bare tool-call node is NOT a decision.
  // Why: dsh appends `tool/call` BEFORE it asks for approval, so the running
  // node already exists when the verdict-ask arrives; keying on its presence
  // rendered every live ask as the settled card, with no buttons to answer it.
  const decided = nodes.some(n => {
    if (n.kind !== 'tool-call') return false;
    const root = (typeof n.data === 'object' && n.data !== null ? n.data : {}) as Record<string, unknown>;
    const block = root.root as Record<string, unknown> | undefined;
    return block?.kind === 'tool-result' && String(block.callId ?? '') === callId;
  });
  if (decided) {
    return (
      <ToolPlate icon={<ShieldAlert size={13} />} title={toolName}>
        <div className="text-ui-sm text-text-dim">{t('verdictBody')}</div>
      </ToolPlate>
    );
  }
  return <VerdictCard pending={{ call_id: callId, tool_name: toolName, message_id: '' }} />;
}

/** The halt card: what stopped the turn and how far it got. The sentence is
 * the row's `content`, never the card's (the plan's Not-doing). */
function HaltNode({ data, nodes, self, onContinue }: {
  data: unknown; nodes: TurnNodeLike[]; self: TurnNodeLike; onContinue?: () => void;
}) {
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const toolCallCount = nodes.filter(n => n.kind === 'tool-call' && n.anchorSeq < self.anchorSeq).length;
  return (
    <HaltCard
      // Same cast the row's no-frames branch makes: the reason is the
      // producer's string, wider than the card's declared union — an unknown
      // reason renders degraded-but-visible (never silent).
      reason={String(d.reason ?? 'unknown') as HaltReason}
      steps={typeof d.steps === 'number' ? d.steps : undefined}
      limit={typeof d.limit === 'number' ? d.limit : undefined}
      toolCallCount={toolCallCount}
      onContinue={String(d.reason) !== 'disconnected' ? onContinue : undefined}
    />
  );
}

/** The detached image run: a RUNNING phase renders the live progress chip; a
 * settled run renders TWO chips — the refined prompt plate and the images
 * plate; a single flat block would lose the prompt chip. The payload is the
 * same node data on BOTH paths (the live frames and the reload mint derive
 * from the same gen_steps dicts). */
function ImageGenNode({ data }: { data: unknown }) {
  const { t } = useTranslation();
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  if (d.status === 'running') {
    // The live phase chip (the run rides the chat channel), rendered from
    // the node's own data. Shown while the run streams; the
    // settled frame folds this node into its settled look.
    const phase = typeof d.phase === 'string' ? d.phase : '';
    return (
      <div className="flex items-center gap-1 text-xs text-text-dim bg-surface2 border border-border-soft px-2 py-1 my-0.5">
        <span className="animate-pulse"><AgentStepIcon /></span>
        <span>{t('generatingImage')}</span>
        {phase && <span className="opacity-60">— {genPhaseLabel(phase, t)}</span>}
      </div>
    );
  }
  if (d.status === 'failed') {
    // Same plate + tone as every other failed step — a failure is a chip, not
    // a stray red line. INVARIANT: the failed plate opens expanded.
    // Why: the cause lives in the body, and a collapsed chip would hide it —
    // the no-silent-degradation rule (a failure must state itself, unclicked).
    return (
      <ToolPlate
        icon={<AgentStepIcon outcome="failed" />}
        title={t('generatedImage')}
        tone="failed"
        defaultExpanded
        ariaLabel={`${t('agentStepFailed')}: ${t('generatedImage')}`}
      >
        <div className="text-ui-sm text-text-dim">
          {t('imageGenerationFailed', { error: String(d.error ?? '') })}
        </div>
      </ToolPlate>
    );
  }
  const ids = Array.isArray(d.imageRefIds) ? d.imageRefIds.map(r => String(r)) : [];
  const refine = (typeof d.refine === 'object' && d.refine !== null ? d.refine : {}) as Record<string, unknown>;
  const refineOk = refine.ok === true;
  const refineFailed = refine.ok === false;
  const refineText = refineOk && typeof refine.prompt === 'string'
    ? refine.prompt
    : refineFailed ? String(refine.error ?? '') : '';
  return (
    <>
      {(refineOk || refineFailed) && (
        <ToolPlate
          icon={<AgentStepIcon outcome={refineFailed ? 'failed' : undefined} />}
          title={t('refinePrompt')}
          tone={refineFailed ? 'failed' : undefined}
          ariaLabel={refineFailed ? `${t('agentStepFailed')}: ${t('refinePrompt')}` : undefined}
        >
          {refineText
            ? <pre className="text-xs font-mono whitespace-pre-wrap break-words max-h-80 overflow-auto bg-surface2 border border-border-soft p-2 m-0">{refineText}</pre>
            : <div className="text-ui-sm text-text-dim">{t('agentStepNoop')}</div>}
        </ToolPlate>
      )}
      <ToolPlate
        icon={<AgentStepIcon />}
        defaultExpanded
        // The harness tool chip's header shape (see ToolCallNode): the label is
        // a protected zone, the title ellipsizes. Without the truncate a long
        // image title wraps the header onto a second line.
        title={typeof d.title === 'string' && d.title
          ? <span className="flex items-center gap-1 min-w-0 w-full">
              <span className="shrink-0">{t('generatedImage')}</span>
              <span className="opacity-50 shrink-0">·</span>
              <span className="truncate min-w-0 flex-1 opacity-70">{d.title}</span>
            </span>
          : <span>{t('generatedImage')}</span>}
      >
        <div className="space-y-1">
          {ids.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {ids.map((id, i) => (
                <GeneratedImageThumb key={id} imageRefId={id} onOpen={() => setOpenIndex(i)} />
              ))}
            </div>
          )}
          {ids.length === 0 && (
            <div className="text-ui-sm text-text-dim">{t('agentStepNoop')}</div>
          )}
        </div>
      </ToolPlate>
      {openIndex !== null && ids.length > 0 && (
        <ImageLightbox
          srcs={ids.map(id => referenceFileUrl(id, 'full.png'))}
          index={openIndex}
          onIndexChange={setOpenIndex}
          onClose={() => setOpenIndex(null)}
          title={t('generatedImage')}
        />
      )}
    </>
  );
}

/** The compaction's mint outcome card (the toast is the live twin, beside the
 * feed — a reload shows the card, never the toast). */
function CompactionMintNode({ data }: { data: unknown }) {
  const { t } = useTranslation();
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const failed = d.mintFailed === true;
  return (
    <ToolPlate
      icon={<AgentStepIcon outcome={failed ? 'failed' : undefined} />}
      title={failed ? t('chatCompactionArchiveFailed') : t('chatContextSummarized')}
      tone={failed ? 'failed' : undefined}
    >
      <div className="text-ui-sm text-text-dim">
        {failed ? String(d.mintReason ?? '') : ''}
      </div>
    </ToolPlate>
  );
}

/** The neutral fallback chip — ANY kind without a dedicated renderer, labelled
 * by its kind. Adding a renderer for a kind is an improvement, never the
 * condition for being visible (the assembler decides visibility upstream). */
function FallbackNode({ node }: { node: TurnNodeLike }) {
  let body: string;
  try { body = JSON.stringify(node.data); } catch { body = ''; }
  return (
    <ToolPlate icon={<FileText size={13} />} title={node.kind}>
      {body
        ? <pre className="text-xs font-mono whitespace-pre-wrap break-words max-h-40 overflow-auto bg-surface2 border border-border-soft p-2 m-0">{body.slice(0, 2000)}</pre>
        : null}
    </ToolPlate>
  );
}

function TurnNode({ node, nodes, isStreaming, onContinue }: {
  node: TurnNodeLike;
  nodes: TurnNodeLike[];
  isStreaming: boolean;
  onContinue?: () => void;
}) {
  const { t } = useTranslation();
  switch (node.kind) {
    case 'assistant-step': return <AssistantStepNode data={node.data} isStreaming={isStreaming} />;
    case 'tool-call': return <ToolCallNode data={node.data} isStreaming={isStreaming} />;
    case 'verdict-ask': return <VerdictAskNode data={node.data} nodes={nodes} />;
    case 'halt': return <HaltNode data={node.data} nodes={nodes} self={node} onContinue={onContinue} />;
    case 'image-gen': return <ImageGenNode data={node.data} />;
    case 'compaction-mint': return <CompactionMintNode data={node.data} />;
    case 'turn-error': {
      const d = (typeof node.data === 'object' && node.data !== null ? node.data : {}) as Record<string, unknown>;
      return (
        <div className="flex items-start gap-2 my-1 text-sm text-red-500">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <div className="min-w-0">
            <div>{String(d.message ?? 'The agent turn failed.')}</div>
            {d.code ? <div className="text-xs opacity-70">{String(d.code)}</div> : null}
          </div>
        </div>
      );
    }
    case 'turn-max-tokens': return (
      <div className="flex items-center gap-1 text-xs text-text-dim my-1">
        <AgentStepIcon outcome="failed" />
        <span>{t('chatHaltReasonOutputTokenLimit')}</span>
      </div>
    );
    // Lore's own rows render the message kinds; the action row stays row-driven.
    case 'user': case 'steering': case 'context': case 'turn-tail':
      return null;
    default: return <FallbackNode node={node} />;
  }
}

/** ONE turn's assembled nodes, in the assembler's order. */
export function TurnNodes({ nodes, isStreaming, onContinue }: {
  nodes: TurnNodeLike[];
  isStreaming: boolean;
  onContinue?: () => void;
}) {
  return (
    <>
      {nodes.map(node => (
        <div key={node.key} className="my-0.5">
          <TurnNode node={node} nodes={nodes} isStreaming={isStreaming} onContinue={onContinue} />
        </div>
      ))}
    </>
  );
}
