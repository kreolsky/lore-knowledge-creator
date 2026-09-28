/** Editor autosave: checkpoint lifecycle over content-sync (entity switch,
 * snapshot enter).
 *
 * ARCH: all writes are delegated to content-sync.ts (the CRDT flush). This
 * hook only manages lifecycle — checkpoint dispatching.
 */
// SYSTEM: autosave — checkpoint lifecycle over content-sync (entity switch, snapshot enter)

import type React from 'react';
import { useRef, useCallback } from 'react';
import type { EditorView } from '@codemirror/view';
import type { WsStatus } from '../collab/ws-status';
import {
  checkpointContent,
  type Item,
  type PersistOptions,
} from '../editor/content-sync';
import { useProjectCollab } from '../collab/ProjectCollabContext';

type ActiveItem = Item | null;

interface UseEditorAutosaveParams {
  editorViewRef: React.RefObject<EditorView | null>;
  getCollabStatus: () => WsStatus | undefined;
}

export function useEditorAutosave({
  editorViewRef,
  getCollabStatus,
}: UseEditorAutosaveParams) {
  const projectCollab = useProjectCollab();

  const getCollabStatusRef = useRef(getCollabStatus);
  getCollabStatusRef.current = getCollabStatus;

  const getOptions = useCallback((): PersistOptions => ({
    collabStatus: getCollabStatusRef.current(),
    projectCollab,
  }), [projectCollab]);

  const checkpoint = useCallback((item: ActiveItem): Promise<void> =>
    checkpointContent(editorViewRef, item, getOptions()), [editorViewRef, getOptions]);

  return { checkpoint };
}
