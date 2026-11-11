/**
 * ArtifactPane — the artifact (html page) column: ArtifactToolbar ONCE over a
 * body slot. Both bodies (the sandboxed frame, the Source code editor) render
 * through this pane, so the Page/Source toggle never remounts the toolbar —
 * its share state (Publish / Published · copy · revoke) survives a toggle.
 */
import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { ArtifactToolbar } from './ArtifactToolbar';

interface ArtifactPaneProps {
  projectId: string;
  documentId: string;
  canEdit: boolean;
  /** true = the Source code editor body; false = the sandboxed frame. */
  source: boolean;
  onToggleSource: () => void;
  children: React.ReactNode;
}

export function ArtifactPane({
  projectId, documentId, canEdit, source, onToggleSource, children,
}: ArtifactPaneProps) {
  const { t } = useTranslation();
  const tRef = useRef(t);
  tRef.current = t;
  const showToast = useAppStore(s => s.showToast);
  const paneRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);

  // WHY: the whole pane goes fullscreen, not the iframe — the toolbar (and its
  // exit button) stays on screen, and the sandboxed frame needs no
  // allowfullscreen. State follows the document event, so Esc is tracked too.
  useEffect(() => {
    const sync = () => setFullscreen(document.fullscreenElement === paneRef.current);
    document.addEventListener('fullscreenchange', sync);
    return () => document.removeEventListener('fullscreenchange', sync);
  }, []);

  const toggleFullscreen = useCallback(() => {
    const request = document.fullscreenElement
      ? document.exitFullscreen()
      : paneRef.current?.requestFullscreen();
    request?.catch(() => showToast(tRef.current('artifactFullscreenFailed'), 'error'));
  }, [showToast]);

  return (
    <div ref={paneRef} className="flex flex-col h-full w-full bg-surface">
      <ArtifactToolbar
        projectId={projectId}
        documentId={documentId}
        canEdit={canEdit}
        source={source}
        onToggleSource={onToggleSource}
        fullscreen={fullscreen}
        onToggleFullscreen={toggleFullscreen}
      />
      <div className="relative flex-1 min-h-0">{children}</div>
    </div>
  );
}
