/**
 * Pure derivation of the document-tree key indicator (plan
 * "glimmering-knitting-pebble"). A document that has the current user's own
 * key configured gets a colored FileText icon: blue = widget capability bound
 * to the doc, red = agent capability scoped to the doc (agent wins when both).
 *
 * Orange (plan "orange-tree-icon") marks a doc covered by a live anonymous
 * public link (`public_share`). Precedence: agent > public > widget > null.
 *
 * Extracted as pure logic so the precedence rule is unit-tested independently
 * of the React tree render.
 */
export interface KeyDocFlags {
  widget: boolean;
  agent: boolean;
  /** Doc covered by a live anonymous public share (owner-only server flag). */
  public?: boolean;
}

/** 'agent' wins over 'public' wins over 'widget' wins over null (no indicator). */
export type KeyIndicator = 'agent' | 'public' | 'widget' | null;

export function deriveKeyIndicator(flags: KeyDocFlags | undefined | null): KeyIndicator {
  if (!flags) return null;
  if (flags.agent) return 'agent';
  if (flags.public) return 'public';
  if (flags.widget) return 'widget';
  return null;
}

/** CSS class applied to the `.doc-icon` span. '' = no key configured. */
export function deriveKeyIconClass(flags: KeyDocFlags | undefined | null): string {
  const ind = deriveKeyIndicator(flags);
  if (ind === 'agent') return 'key-agent';
  if (ind === 'public') return 'key-public';
  if (ind === 'widget') return 'key-widget';
  return '';
}

/** Adapter over the server's `key_capabilities` doc field + `public_share` flag. */
export function keyIconClassFromCapabilities(
  caps: string[] | undefined,
  publicShare?: boolean,
): string {
  if (!caps?.length && !publicShare) return '';
  return deriveKeyIconClass({
    widget: caps?.includes('widget') ?? false,
    agent: caps?.includes('agent') ?? false,
    public: !!publicShare,
  });
}
