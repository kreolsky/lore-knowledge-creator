/** The chip glyph and the generation-phase label — what the node renderer and the
 * live image-gen chip share. */

import { AlertTriangle, FileText } from 'lucide-react';
import { type TranslationKey } from '../../../../i18n';

/** Plan gen-progress-display: localize a generation phase id (refining/queued/
 *  generating/downloading) for the live progress chip. Unknown phases fall back to
 *  the raw id (forward-compat — a new backend phase never breaks the UI). */
export function genPhaseLabel(phase: string, t: (k: TranslationKey) => string): string {
  const map: Record<string, TranslationKey> = {
    refining: 'genPhaseRefining',
    queued: 'genPhaseQueued',
    generating: 'genPhaseGenerating',
    downloading: 'genPhaseDownloading',
  };
  return t(map[phase] ?? (phase as TranslationKey));
}

// The chip icon: a warning triangle for a FAILED step, the neutral tool glyph
// otherwise.
//
// WHY: no branch on the tool NAME. A per-tool glyph is the same layer as a
// per-tool renderer — it must be maintained for every tool that will ever
// exist, and it says nothing the tool name does not.
//
// `shrink-0` is load-bearing, not cosmetic: the header beside it is a tool name
// plus an unbounded args string, and a flex sibling with no shrink floor gets
// squeezed to a sliver by a long one (observed on a `command: curl -sL …` chip).
export function AgentStepIcon({ outcome }: { outcome?: 'failed' }) {
  if (outcome === 'failed') {
    return <AlertTriangle size={13} className="shrink-0 text-amber-400" />;
  }
  return <FileText size={13} className="shrink-0" />;
}
