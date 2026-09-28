/** Sources panel — numbered list of clickable retrieval hits under assistant messages. */

import { memo, useCallback } from 'react';
import { FileText, BookOpen, Link2 } from 'lucide-react';
import { ChatSource } from '../../types';
import { emit } from '../../events';
import { navigateToDocKeepingChat } from '../../chat/navigate';
import { useAppStore } from '../../store/app-store';
import { encodeLinkId } from '../../utils/in-app-link';

interface Props {
  sources: ChatSource[];
}

export const Sources = memo(function Sources({ sources }: Props) {
  // ARCH: source-document click keeps the current chat open across the jump:
  // the destination opens as its BARE body (the navigate-to-document default),
  // not its remembered last-opened reference. setCurrentDocument clears any
  // open reference/preview on doc change.
  // Note: drops the prior preview + offset-auto-scroll; lands at the doc top.
  const handleDocClick = useCallback((source: ChatSource) => {
    navigateToDocKeepingChat(source.id);
  }, []);

  const handleRefClick = useCallback((source: ChatSource) => {
    useAppStore.getState().clearPreviewDocument();
    emit('navigate-to-reference', {
      referenceId: source.id,
      stayInContext: true,
      scrollToOffset: source.offset_start,
    } as { referenceId: string; stayInContext?: boolean; scrollToOffset?: number });
  }, []);

  if (!sources.length) return null;

  return (
    <div className="chat-sources mt-1.5 text-xs text-text-dim">
      <ol className="space-y-0.5">
        {sources.map((s, i) => (
          <li key={i} className="flex items-start gap-1">
            <span className="text-text-muted shrink-0">{i + 1}.</span>
            {s.kind === 'document' ? (
              <span
                role="button"
                tabIndex={0}
                onClick={() => handleDocClick(s)}
                onKeyDown={e => { if (e.key === 'Enter') handleDocClick(s); }}
                className="chat-source inline-flex items-center gap-1 text-accent hover:underline cursor-pointer text-left"
                data-link-type="doc"
                data-link-id={encodeLinkId('doc', s.id)}
              >
                <FileText size={10} className="shrink-0" />
                {s.retrieved && <Link2 size={10} className="shrink-0 text-text-muted" />}
                <span>{s.title}{s.heading ? ` · ${s.heading}` : ''}</span>
              </span>
            ) : (
              <span
                role="button"
                tabIndex={0}
                onClick={() => handleRefClick(s)}
                onKeyDown={e => { if (e.key === 'Enter') handleRefClick(s); }}
                className="chat-source inline-flex items-center gap-1 text-accent hover:underline cursor-pointer text-left"
                data-link-type="ref"
                data-link-id={encodeLinkId('ref', s.id)}
              >
                <BookOpen size={10} className="shrink-0" />
                {s.retrieved && <Link2 size={10} className="shrink-0 text-text-muted" />}
                <span>{s.title}</span>
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
});
