/** Chat mode's follow target: the document of the newest successful agent WRITE in the
 * active chat's assembled conversation (SYSTEM: dsh-conversation). */

import type { TurnNodeLike } from './MessageBubble/internal/turn-nodes';

// INVARIANT: follow fires on WRITES of document text only, from this closed set — never on reads.
// Why: operator ruling — the panel shows the document the agent is changing; a rename, move,
// status flip or a multi/system-document write is not "the document it writes". A mutating
// tool added later is not followed until listed here: the failure is "did not switch", never
// a wrong switch. Names: backend/agent_tools/specs/mutate.py, media.py.
const WRITE_TOOLS_BY_ARG: ReadonlySet<string> = new Set([
  'edit_document', 'append_to_document', 'edit_table_cell', 'create_table',
  'add_table_rows', 'add_table_column', 'restore_document_version', 'replace_artifact_page',
]);
const CREATE_TOOL = 'create_document';

function parseObject(raw: unknown): Record<string, unknown> | null {
  if (typeof raw !== 'string' || raw === '') return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === 'object' && parsed !== null ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function resultObject(content: unknown): Record<string, unknown> | null {
  if (!Array.isArray(content)) return null;
  for (const block of content) {
    const b = block as Record<string, unknown> | null;
    if (b?.type === 'text') return parseObject(b.text);
  }
  return null;
}

/** The written document of one settled tool-call node, or null. */
function writtenDocId(node: TurnNodeLike): string | null {
  if (node.kind !== 'tool-call') return null;
  const block = (node.data as Record<string, unknown> | null)?.root as Record<string, unknown> | undefined;
  if (block?.kind !== 'tool-result' || block.isError === true) return null;
  const call = block.call as Record<string, unknown> | null;
  const name = String(call?.name ?? '');
  const args = parseObject(call?.argsRaw);
  if (WRITE_TOOLS_BY_ARG.has(name)) return typeof args?.document_id === 'string' ? args.document_id : null;
  if (name !== CREATE_TOOL || args?.node_type === 'reference') return null;
  // WHY status check: a confirm-mode proposal settles without 'applied' — follow fires on approval.
  const result = resultObject(block.content);
  return result?.status === 'applied' && typeof result.document_id === 'string' ? result.document_id : null;
}

/** `<nodeKey>|<docId>` of the newest write — a string so a store selector stays stable. */
export function selectFollowTarget(conversation: TurnNodeLike[]): string | null {
  for (let i = conversation.length - 1; i >= 0; i--) {
    const docId = writtenDocId(conversation[i]);
    if (docId) return `${conversation[i].key}|${docId}`;
  }
  return null;
}

export function followTargetDocId(target: string | null): string | null {
  return target ? target.slice(target.indexOf('|') + 1) : null;
}
