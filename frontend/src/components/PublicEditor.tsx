/**
 * PublicEditor — static-seed readonly CM6 viewer for /s/:token.
 *
 * Renders the SAME render-bundle extensions as the live editor (headings,
 * lists, code, transcludes, tables) with two differences:
 *   1. NO collab WS — no `useEditorCollab`, no `useEditorAutosave`, no
 *      `useEditorFileDrop`, no `ProjectCollabContext` consumer. The public page
 *      must NOT open a /ws/collab/project socket (the page is anonymous; opening
 *      one would 401+toast and waste a handshake).
 *   2. Table widgets read from a preview-local Y.Doc seeded by
 *      `currentDocument.tables_json` (captured server-side by /api/public/{token}/documents/{id})
 *      — mirrors the snapshot-preview path in Editor.tsx. Null/missing tables_json
 *      → empty tables map → the widget's readonly missing-model state.
 *
 * Transclusions render inline because `usePublicTransclusionSync` seeds the
 * module-level `transcludeMap` directly from the public store (documents +
 * references), avoiding the authed lazy fetch that 401s anonymously. Image
 * refs embed via `ref-image` entries (carrying public-aware imageUrl);
 * text/doc refs embed via `ref-text`/`doc` entries (lazy-fetched for sibling
 * docs via publicDocumentByDoc).
 *
 * INVARIANT(security): no write affordance — `EditorState.readOnly.of(true)` +
 *   `EditorView.editable.of(false)`. The CM6 state cannot be mutated from the UI.  Why: the public editor is read-only at the CM6 layer (readOnly + editable=false), so the document can't be mutated from the UI regardless of what the server allows.
 *
 * SYSTEM: editor (public-viewer variant) — readonly CM6 viewer for /s/:token.
 */
// ARCH: this component mirrors Editor.tsx's snapshot-preview render branch but
//       as a standalone viewer — no collab, no autosave, no file-drop, no split.
//       Kept separate from Editor.tsx to avoid touching its 670-line hook chain
//       (the plan's highest-risk task); the snapshot-preview branch already
//       proves this render path works without a live collab handle.

import { useMemo, useRef } from 'react';
import { EditorState, Prec, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import CodeMirrorEditor from './editor/CodeMirrorEditor';
import { renderExtensions, editorExperience } from './editor/render-bundle';
import { scrollAnchorPlugin, revealAtCursor } from './editor/live-preview';
import { linkClickExtension } from '../editor/editor-plugins';
import { applyTablesJson } from './editor/live-preview/table-block-model';
import { tableDocSource } from './editor/live-preview/table-doc-source';
import { ReferenceViewerBanner } from './editor/ReferenceViewerBanner';
import { ReferenceMediaBar } from './editor/ReferenceMediaBar';
import { hasReferenceContent } from '../types';
import { useAppStore } from '../store/app-store';
import { useImageReferenceNav } from '../hooks/useImageReferenceNav';
import { usePublicTransclusionSync } from '../hooks/usePublicTransclusionSync';
import { useScrollToLine } from '../hooks/useScrollToLine';
import { useTranslation } from '../i18n';

/** Build a throwaway Y.Doc seeded with a doc's captured `tables_json` (mirrors
 *  Editor.tsx::seedPreviewTablesDoc). Null → empty tables map (readonly
 *  missing-model state in the widget). Never the live editor's doc. */
function seedPublicTablesDoc(tablesJson: string | null | undefined): Y.Doc {
  const doc = new Y.Doc();
  if (tablesJson) applyTablesJson(doc, tablesJson);
  return doc;
}

/** Build the CM6 extension stack for the public /s/:token viewer. Extracted from
 *  the component so the reveal-on-cursor wiring can be asserted in isolation
 *  (PublicEditor.test.ts). Mirrors the snapshot-preview render branch of
 *  Editor.tsx but with no collab/autosave/file-drop. */
export function buildPublicEditorExtensions({ tablesJson }: { tablesJson: string | null | undefined }): Extension[] {
  const renderExt = renderExtensions();
  return [
    editorExperience({ mode: 'document' }),
    renderExt,
    scrollAnchorPlugin,
    // WHY: the click router lives here, not in renderExtensions/editorExperience
    // (Editor.tsx adds it separately). Without it, doc:/ref: link clicks never
    // reach entry.action → navigate-to-reference/document, so in-scope links
    // rendered correctly but did nothing on /s/:token.
    linkClickExtension,
    // Highest precedence overrides — the shared editorExperience defaults
    // editable to true; readonly viewer MUST override (precedence ties lose).
    Prec.highest(EditorState.readOnly.of(true)),
    Prec.highest(EditorView.editable.of(false)),
    // read-only viewer — never reveal raw markdown on selection/caret. The explicit
    // facet override is REQUIRED: editorExperience({mode:'document'}) yields true,
    // and editable:false still gives depth===0 → true (intended for the authed
    // depth-0 snapshot preview). NOTE: plain (NOT Prec.highest) — revealAtCursor
    // combines last-value-wins, and Prec.highest sorts the input to the FRONT of
    // the combine array, where the plain-precedence `true` above it would win.
    // Appended last at default precedence so this `false` is the final value.
    revealAtCursor.of(false),
    // see SYSTEM: table-block — render the captured table state, not a live
    // handle. The public viewer has no collab Yjs doc; seed from tables_json.
    tableDocSource.of(seedPublicTablesDoc(tablesJson)),
  ];
}

export function PublicEditor() {
  const { t } = useTranslation();
  const currentDocument = useAppStore(s => s.currentDocument);
  const currentReference = useAppStore(s => s.currentReference);
  const setCurrentReference = useAppStore(s => s.setCurrentReference);
  const viewRef = useRef<EditorView | null>(null);

  // Arrow-key navigation through image refs (parity with the authed editor).
  // WHY mounted here: when an image ref is open on /s/:token, ArrowLeft/
  // ArrowRight should cycle to the next/prev image ref just like in the
  // authed Editor. The hook reads store state imperatively and calls
  // setCurrentReference — both already valid on the public page. Image refs
  // render via ReferenceMediaBar (no .cm-content), so the focus gate won't
  // block arrows when an image ref is the active item.
  useImageReferenceNav('primary');

  // TOC → heading scroll. WHY mounted here: the TOC on /s/:token emits
  // 'scroll-to-line', but the only subscriber used to be the authed
  // useEditorEvents — the event fired into an empty bus and the TOC was dead.
  // viewRef points at the keyed CM6 instance, so an open reference's TOC
  // scrolls the reference's headings too (same as authed). Always enabled:
  // no split view on the public surface.
  useScrollToLine({ editorViewRef: viewRef });

  // Seed the module-level transcludeMap from public store data so inline
  // transclusions render (images, text refs, docs). Mirrors the authed
  // useEditorReferenceSync shape but with public endpoints and no batch —
  // see the hook for the lazy-doc fetch and clear-on-unmount.
  usePublicTransclusionSync({ editorViewRef: viewRef });

  // Active item: an open reference takes precedence over the doc (mirrors the
  // authed Editor). Its content seeds the CM6 viewer; `key` forces a remount on
  // switch. Refs carry no tables — seed the empty tables map for them.
  const activeRef = currentReference;
  const activeId = activeRef?.reference_id ?? currentDocument?.document_id ?? '';
  const tablesJson = activeRef ? null : (currentDocument?.tables_json ?? null);
  const content = activeRef?.content ?? currentDocument?.content ?? '';
  const isImageRef = activeRef?.media_type === 'image';

  const extensions = useMemo<Extension[]>(
    () => buildPublicEditorExtensions({ tablesJson }),
    // Re-seed only when the active item switches (tablesJson is captured per-doc).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeId],
  );

  // WHY no explicit validity/transclusion effect here: `usePublicTransclusionSync`
  // (mounted above) owns BOTH the link-validity sets (validDocIds/validRefIds)
  // and the module-level `transcludeMap`. The previous inline effect filled
  // only the validity sets — enough for link-coloring but not for inline
  // transclusion rendering, which reads `transcludeMap` exclusively. The hook
  // also dispatches `linkContextChanged` after each rebuild so the livePreview
  // StateField re-runs. Clear-on-unmount is the hook's responsibility too.

  if (!currentDocument) {
    return (
      <div className="flex-1 w-full h-full flex items-center justify-center text-text-dim bg-bg">
        {t('selectDocOrRef')}
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-hidden flex flex-col bg-bg relative">
      {activeRef && (
        <ReferenceViewerBanner
          reference={activeRef}
          isReadonly={true}
          isEmpty={!hasReferenceContent(activeRef)}
          isPublicShare={true}
          onBack={() => setCurrentReference(null)}
        />
      )}
      <div className="editor-scroll">
        {/* Self-gating: renders nothing for a ref without a media file (markdown/docx).
            canEdit=false — the public surface gets the download (public-aware
            referenceFileUrl), never the delete. */}
        {activeRef && <ReferenceMediaBar reference={activeRef} canEdit={false} />}
        {!isImageRef && (
          <div className="editor-content" style={{ minHeight: 0, width: '100%', padding: 0 }}>
            <CodeMirrorEditor
              key={activeId}
              doc={content}
              extensions={extensions}
              onCreateView={(v) => { viewRef.current = v; }}
              className="editor-cm"
            />
          </div>
        )}
      </div>
    </div>
  );
}
