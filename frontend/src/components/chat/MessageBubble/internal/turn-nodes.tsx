/**
 * The assembled turn's renderer — Lore's components over the dsh nodes' data.
 *
 * // SYSTEM: dsh-conversation. The ONE rule: these components READ node data
 * //   and never recompute it — no second fold, no re-derived chip. Each
 * //   `ChatNodeDataMap` kind maps to the Lore component that draws it;
 * //   `user`/`steering`/`context` draw nothing here (Lore's own message rows
 * //   render them), `turn-tail` draws nothing (Lore's action row stays
 * //   row-driven), `turn-process` draws nothing (Lore has no whole-turn
 * //   fold), and ANY other unrendered kind — including `unknown` —
 * //   degrades to the neutral fallback chip. A renderer for a kind is an
 * //   improvement, never the condition for being visible.
 */

import { Fragment, useState } from 'react';
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
import type { ProcessGroupInfo } from '../../../../dsh/lore-conversation.js';

/** One published node, plain-data (the chat store's `conversation` entry). */
export interface TurnNodeLike {
  key: string;
  kind: string;
  anchorSeq: number;
  data: unknown;
  /** dsh's process group holding the node (see ConversationVM). */
  groupKey?: string;
  group?: ProcessGroupInfo;
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
        ? <pre className="text-xs font-mono whitespace-pre-wrap break-words m-0">{body}</pre>
        : <div className="text-ui-sm text-text-dim">{t('agentStepNoop')}</div>}
    </ToolPlate>
  );
}

/** One assistant step: the text and reasoning blocks IN ORDER — the thinking
 * renders WHERE it happened. Tool-call
 * blocks draw nothing (the tool-call node is the chip). `only` draws one
 * part: dsh's process group holds a step's reasoning while its reply stays
 * outside the group. */
function AssistantStepNode({ data, isStreaming, only }: {
  data: unknown; isStreaming: boolean; only?: 'reasoning' | 'text';
}) {
  const { t } = useTranslation();
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const blocks = Array.isArray(d.blocks) ? d.blocks as AssistantBlockLike[] : [];
  const hasText = blocks.some(b => b.kind === 'text' && (b.text ?? '').trim() !== '');
  return (
    <>
      {blocks.map((b, i) => {
        if (b.kind === 'text' && only !== 'reasoning' && (b.text ?? '').trim() !== '') {
          return <MarkdownContent key={i} content={b.text!} streaming={isStreaming} />;
        }
        if (b.kind === 'reasoning' && only !== 'text' && (b.text ?? '').trim() !== '') {
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

/** Whether a verdict ask has been answered: its call settled. */
function isDecidedVerdict(callId: string, nodes: TurnNodeLike[]): boolean {
  // INVARIANT: the ask is decided only once the call's node carries a
  // tool-result — a bare tool-call node is NOT a decision.
  // Why: dsh appends `tool/call` BEFORE it asks for approval, so the running
  // node already exists when the verdict-ask arrives; keying on its presence
  // rendered every live ask as the settled card, with no buttons to answer it.
  return nodes.some(n => {
    if (n.kind !== 'tool-call') return false;
    const root = (typeof n.data === 'object' && n.data !== null ? n.data : {}) as Record<string, unknown>;
    const block = root.root as Record<string, unknown> | undefined;
    return block?.kind === 'tool-result' && String(block.callId ?? '') === callId;
  });
}

/** The verdict ask card. The card STAYS once asked — while the parked call
 * has no tool row it renders the decision card; once the call's own tool-call
 * node exists (the verdict published, the call ran) it renders as a settled
 * record under that row. */
function VerdictAskNode({ data, nodes }: { data: unknown; nodes: TurnNodeLike[] }) {
  const { t } = useTranslation();
  const d = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  const callId = String(d.callId ?? '');
  const toolName = String(d.toolName ?? '');
  if (isDecidedVerdict(callId, nodes)) {
    return (
      <ToolPlate icon={<ShieldAlert size={13} />} title={toolName}>
        <div className="text-ui-sm text-text-dim">{t('verdictBody')}</div>
      </ToolPlate>
    );
  }
  return <VerdictCard pending={{ call_id: callId, tool_name: toolName, message_id: '' }} />;
}

/** The halt card: what stopped the turn and how far it got. The sentence is
 * the row's `content`, never the card's. */
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
      <div className="flex items-center gap-1 text-xs text-text-dim py-1 my-0.5">
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
            ? <pre className="text-xs font-mono whitespace-pre-wrap break-words m-0">{refineText}</pre>
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
        ? <pre className="text-xs font-mono whitespace-pre-wrap break-words m-0">{body.slice(0, 2000)}</pre>
        : null}
    </ToolPlate>
  );
}

function TurnNode({ node, nodes, isStreaming, onContinue, only }: {
  node: TurnNodeLike;
  nodes: TurnNodeLike[];
  isStreaming: boolean;
  onContinue?: () => void;
  only?: 'reasoning' | 'text';
}) {
  const { t } = useTranslation();
  switch (node.kind) {
    case 'assistant-step': return <AssistantStepNode data={node.data} isStreaming={isStreaming} only={only} />;
    case 'tool-call': return <ToolCallNode data={node.data} isStreaming={isStreaming} />;
    case 'verdict-ask': return <VerdictAskNode data={node.data} nodes={nodes} />;
    case 'halt': return <HaltNode data={node.data} nodes={nodes} self={node} onContinue={onContinue} />;
    case 'image-gen': return <ImageGenNode data={node.data} />;
    case 'compaction-mint': return <CompactionMintNode data={node.data} />;
    case 'turn-error': {
      const d = (typeof node.data === 'object' && node.data !== null ? node.data : {}) as Record<string, unknown>;
      // WHY: dsh blanks an AUTH failure's message deliberately (a provider auth
      // error can echo credentials); Lore maps the stable CODE to the actionable
      // sentence here — never the message text (dsh's own routing rule).
      const raw = typeof d.message === 'string' ? d.message.trim() : '';
      const isAuth = d.code === 'AUTH';
      const text = isAuth ? t('chatTurnErrorAuth') : (raw || t('chatHaltReasonError'));
      return (
        <div className="flex items-start gap-2 my-1 text-sm text-red-500">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <div className="min-w-0">
            <div>{text}</div>
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
    // WHY: dsh's whole-turn fold control (counts for a "worked for…" header);
    // Lore does not fold whole turns, so the control has nothing to draw.
    case 'turn-process':
      return null;
    default: return <FallbackNode node={node} />;
  }
}

/** dsh's process-group title: the live activity while the group runs, the
 * top three finished categories once it closes (no counts). */
function groupTitle(info: ProcessGroupInfo, t: ReturnType<typeof useTranslation>['t']): string {
  const { summary } = info;
  if (!info.closed) {
    return summary.running ? t(`processGroupRunning_${summary.running}`) : t('processGroupAnalyzing');
  }
  const labels = summary.counts.slice(0, 3).map(c => t(`processGroupDone_${c.kind}`));
  if (labels.length === 0) return t('processGroupAnalyzed');
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return t('processGroupAnd', { first: labels[0], second: labels[1] });
  const list = labels.join(', ');
  return summary.counts.length > 3 ? t('processGroupEtc', { title: list }) : list;
}

function hasReplyText(node: TurnNodeLike): boolean {
  const d = (typeof node.data === 'object' && node.data !== null ? node.data : {}) as Record<string, unknown>;
  const blocks = Array.isArray(d.blocks) ? d.blocks as AssistantBlockLike[] : [];
  return blocks.some(b => b.kind === 'text' && (b.text ?? '').trim() !== '');
}

function isFailedToolCall(node: TurnNodeLike): boolean {
  if (node.kind !== 'tool-call') return false;
  const d = (typeof node.data === 'object' && node.data !== null ? node.data : {}) as Record<string, unknown>;
  const root = d.root as Record<string, unknown> | undefined;
  return root?.kind === 'tool-result' && root.isError === true;
}

/** One dsh process group as one collapsible plate over its member chips. */
function ProcessGroupNode({ info, members, nodes, isStreaming, onContinue, afterText }: {
  info: ProcessGroupInfo;
  afterText: boolean;
  members: TurnNodeLike[];
  nodes: TurnNodeLike[];
  isStreaming: boolean;
  onContinue?: () => void;
}) {
  const { t } = useTranslation();
  // A failure inside a collapsed group must still show on its header — the
  // no-silent-degradation rule.
  const failed = members.some(isFailedToolCall);
  return (
    // WHY: a group between paragraphs of the answer gets a 24px gap above
    // (my-1's 4px + pt-5) so it does not run into the text; a group opening
    // the answer keeps the plain chip spacing.
    <div className={afterText ? 'pt-5' : undefined}>
      <ToolPlate
        icon={<AgentStepIcon outcome={failed ? 'failed' : undefined} />}
        title={groupTitle(info, t)}
        tone={failed ? 'failed' : undefined}
        bare
        defaultExpanded={isStreaming && !info.closed}
        autoCollapse
        collapseWhen={info.closed}
        isStreaming={isStreaming}
      >
        {members.map(node => (
          <div key={node.key} className="my-0.5">
            <TurnNode node={node} nodes={nodes} isStreaming={isStreaming} onContinue={onContinue} only="reasoning" />
          </div>
        ))}
      </ToolPlate>
    </div>
  );
}

/** Fewer folded members render flat (operator ruling: fold from 2 chips —
 * at most one process chip ever stands unfolded in a run). */
const MIN_GROUP_MEMBERS = 2;

function staysOutOfGroup(node: TurnNodeLike, nodes: TurnNodeLike[]): boolean {
  if (node.kind === 'halt') return true;
  if (node.kind !== 'verdict-ask') return false;
  const d = (typeof node.data === 'object' && node.data !== null ? node.data : {}) as Record<string, unknown>;
  return !isDecidedVerdict(String(d.callId ?? ''), nodes);
}

/** ONE turn's assembled nodes, in the assembler's order. dsh's process groups
 * (read off each node, never recomputed) fold into one plate, drawn where the
 * group's first member stands; a member assistant step contributes its
 * reasoning to the plate and draws its reply at its own place, after it. */
export function TurnNodes({ nodes, isStreaming, onContinue }: {
  nodes: TurnNodeLike[];
  isStreaming: boolean;
  onContinue?: () => void;
}) {
  // INVARIANT: a pending verdict ask and a halt card never fold into a group —
  // they render at their own place, the rest of their group folds around them.
  // Why: a pending decision or a halt must be seen (and answered) without
  // opening anything; a settled verdict is a plain record and folds.
  const outside = new Set(nodes.filter(n => staysOutOfGroup(n, nodes)).map(n => n.key));
  const members = new Map<string, TurnNodeLike[]>();
  for (const node of nodes) {
    if (node.groupKey === undefined || outside.has(node.key)) continue;
    const list = members.get(node.groupKey);
    if (list) list.push(node); else members.set(node.groupKey, [node]);
  }
  const folded = (node: TurnNodeLike): TurnNodeLike[] | undefined => {
    if (node.groupKey === undefined || !node.group || outside.has(node.key)) return undefined;
    const list = members.get(node.groupKey);
    return list && list.length >= MIN_GROUP_MEMBERS ? list : undefined;
  };
  const drawn = new Set<string>();
  // Whether the answer's text already stands above the node being drawn.
  let textAbove = false;
  return (
    <>
      {nodes.map(node => {
        const afterText = textAbove;
        if (node.kind === 'assistant-step' && hasReplyText(node)) textAbove = true;
        const group = folded(node);
        if (!group) {
          return (
            <div key={node.key} className="my-0.5">
              <TurnNode node={node} nodes={nodes} isStreaming={isStreaming} onContinue={onContinue} />
            </div>
          );
        }
        const plate = drawn.has(node.groupKey!) ? null : (
          <ProcessGroupNode
            key={node.groupKey}
            info={node.group!}
            members={group}
            afterText={afterText}
            nodes={nodes}
            isStreaming={isStreaming}
            onContinue={onContinue}
          />
        );
        drawn.add(node.groupKey!);
        // The reply of a member step renders outside the group, after it.
        const reply = node.kind === 'assistant-step' && hasReplyText(node) && (
          <div key={node.key} className="my-0.5">
            <TurnNode node={node} nodes={nodes} isStreaming={isStreaming} onContinue={onContinue} only="text" />
          </div>
        );
        return plate || reply ? <Fragment key={`${node.key}:g`}>{plate}{reply}</Fragment> : null;
      })}
    </>
  );
}
