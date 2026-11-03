/**
 * ArtifactPane — the artifact (html page) column: ArtifactToolbar ONCE over a
 * body slot. Both bodies (the sandboxed frame, the Source code editor) render
 * through this pane, so the Page/Source toggle never remounts the toolbar —
 * its share state (Publish / Published · copy · revoke) survives a toggle.
 */
import type React from 'react';
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
  return (
    <div className="flex flex-col h-full w-full">
      <ArtifactToolbar
        projectId={projectId}
        documentId={documentId}
        canEdit={canEdit}
        source={source}
        onToggleSource={onToggleSource}
      />
      <div className="relative flex-1 min-h-0">{children}</div>
    </div>
  );
}
