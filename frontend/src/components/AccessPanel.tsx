/**
 * AccessPanel — unified access management for the current project + document.
 *
 * SYSTEM: access-panel — the project's access surfaces in one right-tab:
 *   1. General access — project `is_public` toggle + anonymous-link share manager.
 *      Read-only by invariant for everyone-except-owner; the backend enforces
 *      owner-only mint/revoke.
 *   2. Move — move the current document to another project.
 *   3. Inbox — per-(user × document) arrival-notify toggles (SYSTEM: inbox).
 *   4. API — widget/agent keys + transcription agent config for the current
 *      document. Project-wide members live in ProjectSettingsPanel.MembersSection.
 *
 * ARCH: the tab is full-only and hidden when a reference is open (gated in
 * ProjectPage). The collapsed-tab tint (red/yellow/blue) is derived in
 * useAccessTabState; this panel owns the management surface.
 *
 * INVARIANT(security): every write affordance is root-only at the API; the UI
 * hides the triggers AND the handlers early-return for non-roots (never rely on
 * hiding alone — CLAUDE.md).  Why: hiding is not enforcement; the API is root-only AND the handlers early-return for non-roots, so a non-root can't trigger a write via devtools.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { apiClient } from '../api/client';
import { moveDocumentToProject } from '../api/access';
import {
  createShare, listShares, revokeShare, updateShareScope,
  type DocumentShare, type InheritedFrom,
} from '../api/public-share';
import { docUrl } from '../utils/routing';
import { Button, FieldCheckbox, SectionHeader, Dropdown, Modal } from './ui';
import ApiKeyManager from './ApiKeyManager';
import { ParentPickerPopup } from './ParentPickerPopup';
import { copyWithToast } from './chat/shared/copy';
import { useTranslation } from '../i18n';
import { useIsProjectOwner } from '../hooks/useIsProjectOwner';
import { Link2, KeyRound, FolderOutput, Inbox, ChevronDown } from 'lucide-react';
import type { Document, Project } from '../types';
import { deriveShareState, planShareToggle, type ShareAction } from './access/share-state';
import { useInboxStore } from '../store/inbox-store';
import { fetchInboxToggles, type InboxToggles } from '../api/inbox';

export function AccessPanel() {
  const project = useAppStore(s => s.currentProject);
  const currentDocument = useAppStore(s => s.currentDocument);
  const { t } = useTranslation();
  const isOwner = useIsProjectOwner();

  if (!project) return null;
  const docId = currentDocument?.document_id ?? null;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-auto py-1 px-1.5">
        <GeneralAccessSection
          projectId={project.project_id}
          docId={docId}
        />
        {docId && (
          <MoveDocumentSection
            projectId={project.project_id}
            docId={docId}
            docTitle={currentDocument?.title ?? null}
          />
        )}
        {docId && (
          <InboxTogglesSection
            projectId={project.project_id}
            docId={docId}
          />
        )}
        {docId && (
          <>
            <SectionHeader
              // The general-access section above is owner-only; for non-owners the
              // API section becomes the first one rendered.
              first={!isOwner}
              icon={<KeyRound size={11} />}
              title={t('accessApiTitle')}
              description={t('accessApiDesc')}
            />
            <div className="px-3.5">
              <ApiKeyManager documentId={docId} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─── General access: two-checkbox anonymous-link share manager ────────────
//
// One document has at most one share; the UI is two checkboxes:
//   [ ] Share this document      [ ] Include subtree
// State is DERIVED from the server list, so it stays honest after a failed write
// (the optimistic toggle rolls back via refreshShares). A scope change is an atomic
// PATCH, not revoke-then-create. When no own row exists but an ancestor's subtree
// share publishes this doc, the summary is "inherited" and the checkboxes stay off
// but ENABLED — ticking one mints this doc's own root.

/** Apply a toggle plan to the local share list for instant (optimistic) feedback.
 *  Created rows carry placeholder fields: the plaque renders the doc URL (not the
 *  token), and refreshShares replaces them with the real rows on completion. */
function applyOptimistic(shares: DocumentShare[], actions: ShareAction[]): DocumentShare[] {
  let next = shares.slice();
  for (const a of actions) {
    if (a.kind === 'create') {
      next.push({ share_id: '__optimistic__', scope: a.scope, token: '', created_at: '' });
    } else if (a.kind === 'patch') {
      next = next.map(s => (s.share_id === a.shareId ? { ...s, scope: a.scope } : s));
    } else {
      next = next.filter(s => s.share_id !== a.shareId);
    }
  }
  return next;
}


function GeneralAccessSection({
  projectId, docId,
}: {
  projectId: string;
  docId: string | null;
}) {
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);
  const isOwner = useIsProjectOwner();
  const [shares, setShares] = useState<DocumentShare[]>([]);
  const [inheritedFrom, setInheritedFrom] = useState<InheritedFrom | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshShares = useCallback(async () => {
    if (!docId) { setShares([]); setInheritedFrom(null); return; }
    try {
      const data = await listShares(projectId, docId);
      setShares(data.shares);
      setInheritedFrom(data.inherited_from);
    } catch {
      showToast(t('failedToLoadShares'));
    }
  }, [docId, projectId, showToast, t]);

  useEffect(() => { refreshShares(); }, [refreshShares]);

  // INVARIANT(security): the whole anonymous-share section is root-only.
  // Why: user decision — share-link management (list/mint/revoke/scope) is a
  // project-root concern; full-but-not-root collaborators must not see it (the
  // writes are also root-gated on the backend in routes/document_shares.py).
  if (!isOwner) return null;

  const view = deriveShareState(shares, inheritedFrom);

  /** Build the canonical public share URL. plan "public-document-ids": the URL is
   *  /docs/<document_id> (the document uuid), NOT /s/<token> — the token is a legacy
   *  alias that redirects, so scope changes never alter the link people hold. */
  const buildShareUrl = () => docId ? `${window.location.origin}${docUrl(docId)}` : '';

  /** Copy a share URL via the app-wide copyWithToast (unified toast feedback — same
   *  mechanism as every other copy surface; no inline confirmation). */
  const copyShareUrl = (url: string) => copyWithToast(url, showToast);

  /** Optimistic toggle: flip the checkbox instantly, execute the planned actions,
   *  then re-sync from the server. On failure the refresh reverts to the server
   *  truth and the error surfaces — no silent degradation. */
  const handleToggle = async (which: 'shared' | 'subtree') => {
    if (!docId || busy) return;
    const plan = planShareToggle(view, which, shares);
    if (!plan.actions.length) return;
    setShares(applyOptimistic(shares, plan.actions));
    setBusy(true);
    let didCreate = false;
    try {
      for (const a of plan.actions) {
        if (a.kind === 'create') {
          await createShare(projectId, docId, a.scope);
          didCreate = true;
        } else if (a.kind === 'patch') {
          await updateShareScope(a.shareId, a.scope);
        } else {
          await revokeShare(a.shareId);
        }
      }
      // A fresh share: copy the link immediately — the click gesture is present and
      // the link is ready to paste. The plaque remains the re-copy surface for later.
      if (didCreate) copyShareUrl(buildShareUrl());
    } catch {
      showToast(t('accessShareScopeError'), 'error');
    } finally {
      setBusy(false);
      await refreshShares();
    }
  };

  const summary = (() => {
    switch (view.summary) {
      case 'subtree': return t('accessSummarySubtree');
      case 'doc': return t('accessSummaryDoc');
      case 'inherited':
        // INVARIANT(security): never render "Not shared" while a live share resolves.
        // Why: this panel is the owner's only read on public exposure; a false negative
        // misreports access — a security error, not a cosmetic one. A subtree share on
        // an ancestor publishes this doc (_find_share_row in routes/public_share.py).
        return t('accessSummaryInherited', { title: inheritedFrom?.title ?? '' });
      default: return t('accessSummaryNotShared');
    }
  })();

  return (
    <>
      <SectionHeader
        first
        icon={<Link2 size={11} />}
        title={t('accessGeneralTitle')}
        description={t('accessGeneralDesc')}
      />
      <div className="px-3.5">
        {docId ? (
          <div>
            <div className="flex flex-col gap-1.5 mt-1">
              <FieldCheckbox
                checked={view.shared}
                onChange={() => { void handleToggle('shared'); }}
                label={t('accessShareThisDoc')}
              />
              <FieldCheckbox
                checked={view.subtree}
                disabled={view.subtreeDisabled || busy}
                onChange={() => { void handleToggle('subtree'); }}
                label={t('accessIncludeSubtree')}
                title={t('accessIncludeSubtreeHint')}
              />
            </div>

            <p className="text-ui-xs text-text-dim mt-1.5">{summary}</p>
            {view.subtree && (
              <p className="text-ui-xs text-text-dim mt-1">{t('accessShareDepthWarning')}</p>
            )}

            {view.shared && (
              // The link plaque — click to copy (unified toast feedback). The URL is
              // document-keyed, so it is stable across scope changes; it does not
              // depend on the share id.
              <div
                className="flex items-center gap-2 mt-2 w-full cursor-pointer hover:bg-surface3 -mx-1 px-1 py-1 border border-border-soft"
                title={t('accessClickToCopy')}
                onClick={() => { copyShareUrl(buildShareUrl()); }}
              >
                <Link2 size={13} className="shrink-0 text-text-dim" />
                <span className="flex-1 text-ui-xs truncate text-text-dim">
                  {buildShareUrl()}
                </span>
              </div>
            )}
          </div>
        ) : null}
      </div>
    </>
  );
}

// ─── Move to another project — current document + subtree (full-only) ──────
//
// The move is a WRITE on the document, gated as such on BOTH ends by the
// backend (require_document_full on the doc + require_project_full on the
// target) — owner is NOT required here, matching the API's own gating.
// The projects list is fetched on mount (the store's `projects` is seeded only
// by the Dashboard), filtered to my_access === 'full' minus the current
// project: those are exactly the targets the backend would accept.

function MoveDocumentSection({ projectId, docId, docTitle }: {
  projectId: string;
  docId: string;
  docTitle: string | null;
}) {
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [targetId, setTargetId] = useState('');
  const [targetDocs, setTargetDocs] = useState<Document[] | null>(null);
  const [parentId, setParentId] = useState<string | null>(null);
  const [pickerAnchor, setPickerAnchor] = useState<DOMRect | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const parentButtonRef = useRef<HTMLButtonElement>(null);

  // No-silent-degradation: a failed load is an ERROR state, never shown as the
  // "no other writable project" empty state — they must look distinct.
  useEffect(() => {
    let cancelled = false;
    apiClient.get('/projects').then((data: Project[]) => {
      if (cancelled) return;
      setProjects(data.filter(p => p.my_access === 'full' && p.project_id !== projectId));
      setLoadFailed(false);
    }).catch((err) => {
      console.error('Failed to load projects for the move section', err);
      if (cancelled) return;
      setLoadFailed(true);
      useAppStore.getState().showToast(t('accessMoveLoadFailed'), 'error');
    });
    return () => { cancelled = true; };
  }, [projectId, t]);

  // Seed the target from the per-project prefs (last project this user moved a
  // doc INTO from here), falling back to the first writable one so the Dropdown
  // trigger never shows an unselected option's label (it seeds its index to 0
  // on no match). A remembered id that is no longer writable is ignored.
  const lastMoveTarget = useUIStore(s => s.lastMoveTargetProjectId);
  useEffect(() => {
    if (targetId || projects.length === 0) return;
    const remembered = projects.find(p => p.project_id === lastMoveTarget);
    setTargetId((remembered ?? projects[0]).project_id);
  }, [projects, targetId, lastMoveTarget]);

  const selectTarget = (id: string) => {
    setTargetId(id);
    useUIStore.getState().setLastMoveTargetProject(id);
  };

  // Target's tree for the parent picker: full rows from GET /projects/{target};
  // system rows are filtered CLIENT-side so the agent skeleton is not offered
  // (the backend still refuses via assert_parent_valid + the is_system parent
  // check in documents/move.py).
  useEffect(() => {
    setParentId(null);
    setTargetDocs(null);
    if (!targetId) return;
    let cancelled = false;
    apiClient.get(`/projects/${targetId}`).then((data: { documents: Document[] }) => {
      if (!cancelled) setTargetDocs(data.documents.filter(d => !d.is_system));
    }).catch((err) => {
      console.error('Failed to load target project tree', err);
      useAppStore.getState().showToast(t('accessMoveLoadFailed'), 'error');
    });
    return () => { cancelled = true; };
  }, [targetId, t]);

  const targetProject = projects.find(p => p.project_id === targetId) ?? null;
  const parentTitle = parentId
    ? targetDocs?.find(d => d.document_id === parentId)?.title ?? ''
    : t('accessMoveRootLabel');

  const handleMove = async () => {
    if (!targetId || busy) return;
    setBusy(true);
    try {
      await moveDocumentToProject(docId, {
        target_project_id: targetId, parent_id: parentId,
      });
      setConfirming(false);
      // No success handling here ON PURPOSE: this client is a source-project
      // client like any other — it receives documents_moved_out over the
      // project WS and follows the doc via the hard /docs/<id> reload (Sidebar).
    } catch (err) {
      console.error('Failed to move document', err);
      showToast(t('accessMoveFailed'), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <SectionHeader
        icon={<FolderOutput size={11} />}
        title={t('accessMoveTitle')}
        description={t('accessMoveDesc')}
      />
      <div className="px-3.5">
        {loadFailed ? (
          <p className="text-ui-xs text-red mt-1">{t('accessMoveLoadFailed')}</p>
        ) : projects.length === 0 ? (
          <p className="text-ui-xs text-text-dim mt-1">{t('accessMoveNoWritableProjects')}</p>
        ) : (
          <div className="flex flex-col gap-1.5 mt-1">
            <div className="flex items-center gap-2">
              <span className="text-ui-xs text-text-dim w-[110px] shrink-0">{t('accessMoveTargetProject')}</span>
              <Dropdown
                value={targetId}
                options={projects.map(p => ({ value: p.project_id, label: p.name }))}
                onSelect={selectTarget}
              />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-ui-xs text-text-dim w-[110px] shrink-0">{t('accessMoveTargetParent')}</span>
              {/* Same plate as the Dropdown trigger above (composer variant + chevron):
                  the two selectors read as one control family. */}
              <Button
                ref={parentButtonRef}
                variant="composer"
                size="sm"
                disabled={!targetDocs}
                onClick={() => {
                  const rect = parentButtonRef.current?.getBoundingClientRect();
                  if (rect) setPickerAnchor(rect);
                }}
              >
                <span className="truncate max-w-[180px]">{parentTitle}</span>
                <ChevronDown size={12} className="ml-1 opacity-60 shrink-0" />
              </Button>
            </div>
            {/* The action sits under the plates with the same label gutter, so the
                left edges of project → parent → Move line up: one vertical path. */}
            <div className="flex items-center gap-2">
              <span className="w-[110px] shrink-0" />
              <Button
                variant="primary"
                size="sm"
                disabled={!targetId || !targetDocs}
                onClick={() => setConfirming(true)}
              >
                {t('accessMoveButton')}
              </Button>
            </div>
          </div>
        )}
      </div>
      {pickerAnchor && targetDocs && (
        <ParentPickerPopup
          currentDocumentId={parentId}
          onMoved={(newParentId) => setParentId(newParentId)}
          anchorRect={pickerAnchor}
          onClose={() => setPickerAnchor(null)}
          documents={targetDocs}
          noParentLabel={t('accessMoveRootLabel')}
        />
      )}
      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        title={t('accessMoveTitle')}
        footer={
          <>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>{t('cancel')}</Button>
            <Button variant="primary" size="sm" disabled={busy} onClick={handleMove}>
              {t('accessMoveButton')}
            </Button>
          </>
        }
      >
        <p className="text-ui-sm text-text">
          {t('accessMoveConfirm', {
            title: docTitle ?? '', project: targetProject?.name ?? '',
          })}
        </p>
      </Modal>
    </>
  );
}

// ─── Inbox arrival toggles — per (user × document) notify prefs ─────────────
//
// see SYSTEM: inbox. Ruling: the toggles
// live in the Access tab — it is the per-document settings surface, and the
// audience is full-only recipients like every other write affordance here (the
// tab itself is full-only, gated in ProjectPage — no second gate owed).
// The defaults mirror the backend's (notes ON, refs OFF — the same pair a
// missing user_preferences row resolves to). GET seeds the store slice the
// checkboxes read; a flip is an optimistic seedToggles + PUT (updateToggles)
// that REVERTS to the exact prior pair and toasts on failure — no silent
// degradation.

const INBOX_TOGGLE_DEFAULTS: InboxToggles = { notes: true, refs: false };

export function InboxTogglesSection({ projectId, docId }: { projectId: string; docId: string }) {
  const { t } = useTranslation();
  const toggles = useInboxStore(s => s.toggles[`${projectId}:${docId}`]) ?? null;
  // i18n rule: `t` is a NEW function every render — read it in effects/handlers
  // via a ref, never through the dep arrays.
  const tRef = useRef(t);
  tRef.current = t;

  useEffect(() => {
    let cancelled = false;
    fetchInboxToggles(projectId, docId)
      .then((value) => {
        if (!cancelled) useInboxStore.getState().seedToggles(projectId, docId, value);
      })
      .catch((err) => {
        console.error('Failed to load inbox toggles', err);
        if (!cancelled) {
          useAppStore.getState().showToast(tRef.current('inboxToggleLoadFailed'), 'error');
        }
      });
    return () => { cancelled = true; };
  }, [projectId, docId]);

  const flip = (kind: keyof InboxToggles, next: boolean) => {
    const prev = toggles ?? INBOX_TOGGLE_DEFAULTS;
    useInboxStore.getState().seedToggles(projectId, docId, { ...prev, [kind]: next });
    useInboxStore.getState().updateToggles(projectId, docId, { [kind]: next })
      .catch((err) => {
        console.error('Failed to save inbox toggle', err);
        useInboxStore.getState().seedToggles(projectId, docId, prev);
        useAppStore.getState().showToast(tRef.current('inboxToggleSaveFailed'), 'error');
      });
  };

  const value = toggles ?? INBOX_TOGGLE_DEFAULTS;

  return (
    <>
      <SectionHeader
        icon={<Inbox size={11} />}
        title={t('accessInboxTitle')}
        description={t('accessInboxDesc')}
      />
      <div className="px-3.5">
        <div className="flex flex-col gap-1.5 mt-1">
          <FieldCheckbox
            checked={value.notes}
            onChange={(v) => flip('notes', v)}
            label={t('inboxToggleNotes')}
          />
          <FieldCheckbox
            checked={value.refs}
            onChange={(v) => flip('refs', v)}
            label={t('inboxToggleRefs')}
          />
        </div>
      </div>
    </>
  );
}
