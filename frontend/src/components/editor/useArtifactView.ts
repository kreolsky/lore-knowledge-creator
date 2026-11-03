/**
 * useArtifactView — the artifact (media_type 'html') render state for Editor:
 * the Page/Source toggle (incl. the view-registry teardown when the Source
 * view unmounts), the Source-view CM6 extensions (with the human writer's
 * byte cap), the live change seq that reloads the frame, and the signed-URL
 * mint. Extracted from Editor.tsx; the layout consuming it is ArtifactPane.
 *
 * The collab join runs for an artifact like for any document; the frame
 * reloads off the joined ytext, and the Source view binds to it through the
 * same compartments as the markdown editor.
 */
import type React from 'react';
import { useCallback, useMemo, useState } from 'react';
import type { Compartment } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { apiClient } from '../../api/client';
import { unmountView, setRoleView, type EditorRole } from '../../editor/active-editor';
import { useAppStore } from '../../store/app-store';
import { t } from '../../i18n';
import { isArtifactDoc } from '../../utils/artifact';
import type { Document, Reference } from '../../types';
import type { EntityYjsState } from '../../collab/yjs-provider';
import { useYtextChangeSeq, type ArtifactUrl } from './ArtifactFrame';
import { buildArtifactSourceExtensions } from './artifact-source-extensions';

type ActiveItem = Document | Reference | null;

/** The cap in human units — mirrors backend/artifacts.py `_cap_label` (a whole
 *  MiB renders MiB), so the toast names the same number the server's 422 does. */
function capLabel(maxBytes: number): string {
  if (maxBytes % (1024 * 1024) === 0) return `${maxBytes / (1024 * 1024)} MiB`;
  return `${Math.floor(maxBytes / 1024)} KiB`;
}

export interface UseArtifactViewParams {
  activeItem: ActiveItem;
  activeItemId: string | undefined;
  isReference: boolean;
  /** The editor's yCollab compartment — the Source view binds through it. */
  yjsCompartment: React.RefObject<Compartment>;
  liveForCurrent: boolean;
  /** The editor's collab connection ref — the frame's changeSeq source. */
  collabConnectionRef: React.RefObject<EntityYjsState | null>;
  editorViewRef: React.RefObject<EditorView | null>;
  roleRef: React.RefObject<EditorRole>;
}

export interface UseArtifactViewResult {
  isArtifact: boolean;
  artifactSource: boolean;
  artifactCmExtensions: ReturnType<typeof buildArtifactSourceExtensions>;
  artifactChangeSeq: number;
  fetchArtifactUrl: () => Promise<ArtifactUrl>;
  toggleArtifactSource: () => void;
}

export function useArtifactView({
  activeItem, activeItemId, isReference, yjsCompartment, liveForCurrent,
  collabConnectionRef, editorViewRef, roleRef,
}: UseArtifactViewParams): UseArtifactViewResult {
  const isArtifact = !isReference && isArtifactDoc(activeItem as Document | null);
  const [artifactSourceFor, setArtifactSourceFor] = useState<string | null>(null);
  const artifactSource = isArtifact && artifactSourceFor === activeItemId;

  const onOversize = useCallback((maxBytes: number) => {
    useAppStore.getState().showToast(t('artifactTooLarge', { max: capLabel(maxBytes) }), 'error');
  }, []);

  const artifactCmExtensions = useMemo(
    () => buildArtifactSourceExtensions(
      yjsCompartment.current,
      // Read at transaction time: a refreshed /me lands without rebuilding the
      // extensions, which are created once per editor.
      () => useAppStore.getState().currentUser?.artifact_max_bytes ?? null,
      onOversize,
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const artifactChangeSeq = useYtextChangeSeq(
    isArtifact && liveForCurrent ? collabConnectionRef.current?.ytext ?? null : null,
  );

  const fetchArtifactUrl = useCallback(
    (): Promise<ArtifactUrl> => apiClient.get(`/documents/${activeItemId}/artifact-url`),
    [activeItemId],
  );

  const toggleArtifactSource = useCallback(() => {
    if (artifactSource) {
      // The Source view unmounts with the toggle; drop it from every registry so the
      // collab binding and the editor slots never act on a destroyed view.
      const view = editorViewRef.current;
      editorViewRef.current = null;
      setRoleView(roleRef.current, null, view ?? undefined);
      unmountView(view ?? undefined);
    }
    setArtifactSourceFor(artifactSource ? null : activeItemId ?? null);
  }, [artifactSource, activeItemId, editorViewRef, roleRef]);

  return {
    isArtifact, artifactSource, artifactCmExtensions, artifactChangeSeq,
    fetchArtifactUrl, toggleArtifactSource,
  };
}
