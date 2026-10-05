/** Table of contents: prefers live (client-derived) headings when they match the
 * active entity, falling back to server-computed headings. Store reads:
 * currentDocument, currentReference, liveHeadings. Events: scroll-to-line.
 *
 * Renders an Obsidian-style collapsible tree: the flat HeadingItem[] (h1–h4) is
 * nested by level; headings with sub-headings get a chevron. No dots, no icons.
 */
// ARCH: liveHeadings overrides server headings while editing so the TOC reflects
// the live CM6 doc in real time; it is gated by entityId === activeEntityId so a
// secondary-column write for a different entity cannot leak into this TOC.

import { useMemo, useState, useEffect } from 'react';
import { ChevronRight, ChevronDown, Link } from 'lucide-react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { emit } from '../events';
import { useTranslation } from '../i18n';
import { copyWithToast } from './chat/shared/copy';
import { assignHeadingSlugs, buildHeadingUrl } from '../utils/heading-slug';
import { IconButton } from './ui';
import type { HeadingItem } from '../types';
import s from './Sidebar.module.css';

interface TocNode extends HeadingItem {
  children: TocNode[];
}

/** Nest a flat heading list by level using an ancestor stack. A heading nests
 * under the nearest preceding heading with a strictly smaller level; skipped
 * levels (e.g. h1 → h3) are handled by comparing levels, not assuming +1 steps. */
function buildTocTree(items: HeadingItem[]): TocNode[] {
  const roots: TocNode[] = [];
  const stack: TocNode[] = [];
  for (const item of items) {
    const node: TocNode = { ...item, children: [] };
    while (stack.length > 0 && stack[stack.length - 1].level >= node.level) {
      stack.pop();
    }
    if (stack.length === 0) {
      roots.push(node);
    } else {
      stack[stack.length - 1].children.push(node);
    }
    stack.push(node);
  }
  return roots;
}

function TocItem({
  node,
  level,
  collapsed,
  onToggle,
  onSelect,
  onCopy,
  copyTitle,
}: {
  node: TocNode;
  level: number;
  collapsed: Set<number>;
  onToggle: (line: number) => void;
  onSelect: (line: number) => void;
  onCopy?: (line: number) => void;
  copyTitle?: string;
}) {
  const hasChildren = node.children.length > 0;
  const isCollapsed = collapsed.has(node.line);

  return (
    <div>
      <div
        className={`toc-item group relative overflow-hidden ${level === 0 ? 'toc-root' : ''}`}
        style={level > 0 ? { paddingLeft: level * 12 + 4 } : undefined}
        onClick={() => onSelect(node.line)}
      >
        {hasChildren ? (
          // WHY: wide chevron hitbox so collapse/expand is easy to hit without
          // aiming at the tiny chevron — chevron zone toggles, rest of the row scrolls.
          <div
            className="flex items-center shrink-0 cursor-pointer text-text-dim"
            style={(() => { const pad = level > 0 ? level * 12 + 4 : 4; return { marginLeft: -pad, paddingLeft: pad }; })()}
            onClick={(e) => { e.stopPropagation(); onToggle(node.line); }}
          >
            {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
          </div>
        ) : (
          <span className="w-3 shrink-0" />
        )}
        {/* Same clipping scheme as the document list's .doc-label: flex-1 + ellipsis. */}
        <span className="flex-1 truncate">{node.text}</span>
        {onCopy && (
          // Hover plate over the row edge (DocumentTree/RowActions scheme): text is
          // clipped by the row edge (no ellipsis), the icon fades in on a gradient
          // backdrop matching the row's hover fill.
          <div
            className="absolute right-0 top-0 bottom-0 flex items-center pl-4 pr-0.5 opacity-0 group-hover:opacity-100 bg-[linear-gradient(to_right,transparent_0px,var(--surface3)_16px)]"
            onClick={(e) => e.stopPropagation()}
          >
            <IconButton
              size="sm"
              title={copyTitle}
              aria-label={copyTitle}
              onClick={() => onCopy(node.line)}
            >
              <Link size={12} />
            </IconButton>
          </div>
        )}
      </div>
      {hasChildren && !isCollapsed && (
        <div>
          {node.children.map((child, idx) => (
            <TocItem
              key={`${child.line}-${idx}`}
              node={child}
              level={level + 1}
              collapsed={collapsed}
              onToggle={onToggle}
              onSelect={onSelect}
              onCopy={onCopy}
              copyTitle={copyTitle}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function TableOfContents() {
  const { t } = useTranslation();
  const currentDocument = useAppStore(s => s.currentDocument);
  const rawReference = useAppStore(s => s.currentReference);
  const liveHeadings = useAppStore(s => s.liveHeadings);
  // Panel quick preview: the TOC is the DOCUMENT's — read the open reference
  // through the ONE projection so headings + copy-links follow the doc scope.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? ''], s.compactLayout));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;

  const activeEntityId = currentReference?.reference_id ?? currentDocument?.document_id;

  const headings = useMemo<HeadingItem[]>(() => {
    if (liveHeadings && liveHeadings.entityId === activeEntityId) {
      return liveHeadings.items;
    }
    if (currentReference) return currentReference.headings || [];
    return currentDocument?.headings || [];
  }, [currentDocument?.headings, currentReference?.headings, liveHeadings, activeEntityId]);

  const tree = useMemo(() => buildTocTree(headings), [headings]);

  // Ephemeral collapse state (Obsidian-style): collapsed heading line numbers,
  // reset on document switch so a new doc opens fully expanded.
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  useEffect(() => { setCollapsed(new Set()); }, [activeEntityId]);

  const handleToggle = (line: number) => {
    setCollapsed(prev => {
      const next = new Set(prev);
      if (next.has(line)) next.delete(line); else next.add(line);
      return next;
    });
  };

  const handleSelect = (line: number) => {
    emit('scroll-to-line', { line });
  };

  // Copy affordances exist only for the current document — references have no
  // standalone URL route, so a shareable link cannot target them.
  const currentProject = useAppStore(st => st.currentProject);
  const canCopyLinks = !currentReference && !!currentDocument && !!currentProject;
  const showToast = useAppStore(st => st.showToast);

  const handleCopyHeading = (line: number) => {
    if (!canCopyLinks || !currentProject || !currentDocument) return;
    const slugged = assignHeadingSlugs(headings);
    const target = slugged.find(item => item.line === line);
    if (!target) return;
    copyWithToast(buildHeadingUrl(currentDocument.document_id, target.slug), showToast);
  };

  const activeItem = currentReference || currentDocument;

  if (!activeItem) {
    return (
      <div className={s.sidebarToc}>
        <div className="p-3 px-2 text-xs text-text-dim">
          {t('noDocumentSelected')}
        </div>
      </div>
    );
  }

  if (headings.length === 0) {
    return (
      <div className={s.sidebarToc}>
        <div className="p-3 px-2 text-xs text-text-dim">
          {t('noHeadingsFound')}
        </div>
      </div>
    );
  }

  return (
    <div className={s.sidebarToc}>
      {tree.map((node, idx) => (
        <TocItem
          key={`${node.line}-${idx}`}
          node={node}
          level={0}
          collapsed={collapsed}
          onToggle={handleToggle}
          onSelect={handleSelect}
          onCopy={canCopyLinks ? handleCopyHeading : undefined}
          copyTitle={t('copyHeadingLink')}
        />
      ))}
    </div>
  );
}
