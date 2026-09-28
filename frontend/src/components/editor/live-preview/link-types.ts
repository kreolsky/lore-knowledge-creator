/**
 * Single source of link-type knowledge for the editor.
 *
 * ARCH: every link type (note, ref, doc, ext, bare) is described once here as a
 * registry entry: how to MATCH it in a line of text (with group-index knowledge
 * kept INSIDE the entry), how to VALIDATE its id, what ACTION to dispatch on
 * click, its DECORATION class, and its BROKEN tooltip. Both consumers read only
 * the registry:
 *   - editor-plugins.ts click handler iterates LINK_TYPES instead of 5 inline loops.
 *   - build-structural.ts resolveLinkClass / brokenTooltip delegate here.
 *
 * Capture-group contract (PINNED): the 5 source
 * regexes have heterogeneous group structures (note/ref use match[2] + prefix-strip,
 * doc/ext use match[2], bare uses match[0]). To keep `resolve`/`action` uniform,
 * each entry's `match(lineText)` returns a NORMALIZED `{ start, end, raw, id }` —
 * `start`/`end` are line-relative offsets, `raw` is the full matched text, `id` is
 * the already-stripped identifier/url. Per-entry group-index and prefix-strip
 * knowledge lives inside `match` / `extractId`; it NEVER leaks into the click
 * router. `tryLink` consumes only the normalized shape.
 *
 * Validity: `link-validity.ts` keeps storing the validity Sets; the registry is
 * their only reader (writer remains `useEditorReferenceSync`).
 */
// SYSTEM: link-types — registry of link types (single source for match/resolve/validate/action/class/tooltip)

import { noteLink, refLink, docLink, extLink, bareUrl } from '../link-patterns';
import { validDocIds, validNoteThreadIds, validRefIds, projectRefIds } from './link-validity';
import { useAppStore } from '../../../store/app-store';
import { emit } from '../../../events';
import { t } from '../../../i18n';

export type LinkKind = 'note' | 'ref' | 'doc' | 'ext' | 'bare';

/** Normalized link match — line-relative offsets + stripped id. */
export interface LinkMatch {
  start: number;
  end: number;
  raw: string;
  id: string;
}

export interface LinkTypeEntry {
  kind: LinkKind;
  /** Scheme prefix stripped to obtain the id ('note:' / 'ref:' / undefined). */
  prefix?: string;
  /** Find all matches in a line of text, normalized to { start, end, raw, id }. */
  match(lineText: string): LinkMatch[];
  /** Strip the scheme prefix from a raw urlText → bare id/url. */
  extractId(urlText: string): string;
  /** Is this id resolvable right now? (reads the validity Sets) */
  validate(id: string): boolean;
  /** Action dispatched on a successful (valid) click. */
  action(id: string): void;
  /** Base decoration class (without the -broken suffix). */
  decorationClass: string;
  /**
   * Tooltip shown when the link is broken (validate === false). Lazy: calls the
   * i18n `t()` at invocation time (NOT at module-eval) so test mocks of i18n that
   * omit the `t` export don't break module loading of consumers.
   */
  brokenTooltip(): string;
}

const NOTE: LinkTypeEntry = {
  kind: 'note',
  prefix: 'note:',
  match(lineText) {
    const out: LinkMatch[] = [];
    const re = noteLink();
    let m: RegExpExecArray | null;
    while ((m = re.exec(lineText)) !== null) {
      out.push({ start: m.index, end: m.index + m[0].length, raw: m[0], id: m[2].replace('note:', '') });
    }
    return out;
  },
  extractId(urlText) { return urlText.replace('note:', ''); },
  validate(id) { return validNoteThreadIds.has(id); },
  action(id) {
    emit('open-notes', { threadId: id });
    setTimeout(() => emit('highlight-note', { noteId: id }), 100);
    setTimeout(() => emit('connect-note', { noteId: id }), 200);
  },
  decorationClass: 'cm-note-link',
  brokenTooltip: () => t('linkBrokenNote'),
};

const REF: LinkTypeEntry = {
  kind: 'ref',
  prefix: 'ref:',
  match(lineText) {
    const out: LinkMatch[] = [];
    const re = refLink();
    let m: RegExpExecArray | null;
    while ((m = re.exec(lineText)) !== null) {
      out.push({ start: m.index, end: m.index + m[0].length, raw: m[0], id: m[2].replace('ref:', '') });
    }
    return out;
  },
  extractId(urlText) { return urlText.replace('ref:', ''); },
  validate(id) { return validRefIds.has(id) || projectRefIds.has(id); },
  action(id) { emit('navigate-to-reference', { referenceId: id }); },
  decorationClass: 'cm-ref-link',
  brokenTooltip: () => t('linkBrokenReference'),
};

const DOC: LinkTypeEntry = {
  kind: 'doc',
  match(lineText) {
    const out: LinkMatch[] = [];
    const re = docLink();
    let m: RegExpExecArray | null;
    while ((m = re.exec(lineText)) !== null) {
      out.push({ start: m.index, end: m.index + m[0].length, raw: m[0], id: m[2] });
    }
    return out;
  },
  extractId(urlText) { return urlText; },
  validate(id) { return validDocIds.has(id); },
  action(id) { emit('navigate-to-document', { documentId: id }); },
  decorationClass: 'cm-doc-link',
  brokenTooltip: () => t('linkBrokenDocument'),
};

const EXT: LinkTypeEntry = {
  kind: 'ext',
  match(lineText) {
    const out: LinkMatch[] = [];
    const re = extLink();
    let m: RegExpExecArray | null;
    while ((m = re.exec(lineText)) !== null) {
      out.push({ start: m.index, end: m.index + m[0].length, raw: m[0], id: m[2] });
    }
    return out;
  },
  extractId(urlText) { return urlText; },
  validate() { return true; },
  action(url) { window.open(url, '_blank', 'noopener,noreferrer'); },
  decorationClass: 'cm-ext-link',
  brokenTooltip: () => '',
};

const BARE: LinkTypeEntry = {
  kind: 'bare',
  match(lineText) {
    const out: LinkMatch[] = [];
    const re = bareUrl();
    let m: RegExpExecArray | null;
    while ((m = re.exec(lineText)) !== null) {
      out.push({ start: m.index, end: m.index + m[0].length, raw: m[0], id: m[0] });
    }
    return out;
  },
  extractId(urlText) { return urlText; },
  validate() { return true; },
  action(url) { window.open(url, '_blank', 'noopener,noreferrer'); },
  decorationClass: 'cm-ext-link',
  brokenTooltip: () => '',
};

/** Ordered registry consumed by the click router. */
export const LINK_TYPES: LinkTypeEntry[] = [NOTE, REF, DOC, EXT, BARE];

const ENTRY_BY_CLASS = new Map<string, LinkTypeEntry>(
  LINK_TYPES.map((e) => [e.decorationClass, e]),
);

/**
 * Resolve a base decoration class to its valid/broken form for a raw urlText.
 * Delegates validity to the matching registry entry. Used by build-structural.
 * Returns the class unchanged when no entry matches (e.g. plain cm-link).
 */
export function resolveLinkClass(cls: string, urlText: string): string {
  const entry = ENTRY_BY_CLASS.get(cls);
  if (!entry) return cls;
  return entry.validate(entry.extractId(urlText)) ? cls : `${cls}-broken`;
}

/**
 * Broken-link tooltip for a class. Mirrors the original build-structural helper:
 * returns a tooltip ONLY when the class carries the `-broken` suffix (the only
 * form build-structural passes in); '' for base classes and unknown types.
 */
export function linkBrokenTooltip(cls: string): string {
  if (!cls.endsWith('-broken')) return '';
  const base = cls.slice(0, -'-broken'.length);
  const entry = ENTRY_BY_CLASS.get(base);
  return entry ? entry.brokenTooltip() : '';
}

/**
 * Show a broken-link toast for a base decoration class (as carried by a registry
 * entry, e.g. 'cm-note-link'). No-op for types with no tooltip (ext/bare) or
 * unknown classes. Used by the click router.
 */
export function showBrokenToast(cls: string): void {
  const entry = ENTRY_BY_CLASS.get(cls);
  if (!entry) return;
  const msg = entry.brokenTooltip();
  if (!msg) return;
  useAppStore.getState().showToast(msg, 'warning');
}
