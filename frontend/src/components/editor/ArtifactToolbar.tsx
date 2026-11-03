/**
 * Toolbar over an artifact (html page) in the window: the Page/Source switch
 * (members who may edit), the html download (every reader) and the one-click publication (project owner) —
 * "Publish", or once a link exists "Published · copy · revoke". Publication is
 * the ordinary doc-scope document_shares link (see SYSTEM: public-share); this
 * is a second trigger for it, not a second mechanism.
 *
 * The published state is DERIVED from the server list (deriveShareState, same
 * as the Access panel) and refetched on `shares:changed`, so a share
 * minted/revoked in the Access panel moves this toolbar without a remount.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Globe } from 'lucide-react';
import { Button, DownloadMenu } from '../ui';
import { useAppStore } from '../../store/app-store';
import { useIsProjectOwner } from '../../hooks/useIsProjectOwner';
import { useEvent } from '../../hooks/useEvent';
import { createShare, listShares, revokeShare, type SharesListResponse } from '../../api/public-share';
import { deriveShareState } from '../access/share-state';
import { copyWithToast } from '../chat/shared/copy';
import { docUrl } from '../../utils/routing';
import { useTranslation } from '../../i18n';
import { ARTIFACT_EXPORT_FORMATS } from '../../utils/artifact';

interface ArtifactToolbarProps {
  projectId: string;
  documentId: string;
  canEdit: boolean;
  source: boolean;
  onToggleSource: () => void;
}

export function ArtifactToolbar({ projectId, documentId, canEdit, source, onToggleSource }: ArtifactToolbarProps) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 px-2 py-1 border-b border-border bg-surface shrink-0">
      {canEdit && (
        <Button variant="ghost" size="sm" onClick={onToggleSource}>
          {source ? t('artifactShowPage') : t('artifactShowSource')}
        </Button>
      )}
      <DownloadMenu documentId={documentId} hasText exportFormats={ARTIFACT_EXPORT_FORMATS} />
      <div className="flex-1" />
      <PublishControl projectId={projectId} documentId={documentId} />
    </div>
  );
}

function PublishControl({ projectId, documentId }: { projectId: string; documentId: string }) {
  const { t } = useTranslation();
  const tRef = useRef(t);
  tRef.current = t;
  const showToast = useAppStore(s => s.showToast);
  const isOwner = useIsProjectOwner();
  const [shares, setShares] = useState<SharesListResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setShares(await listShares(projectId, documentId));
    } catch {
      showToast(tRef.current('failedToLoadShares'), 'error');
    }
  }, [projectId, documentId, showToast]);

  useEffect(() => {
    if (isOwner) void refresh();
  }, [isOwner, refresh]);

  // A share changed elsewhere (the Access panel, this toolbar's own writers
  // through the same chokepoint) — re-derive from a fresh list.
  useEvent('shares:changed', () => { void refresh(); });

  // INVARIANT(security): owner only — hidden here AND refused server-side
  // (routes/document_shares.py, ensure_owner_for_doc). Why: share management is a
  // project-root concern; hiding alone is not enforcement.
  if (!isOwner || !shares) return null;

  const link = `${window.location.origin}${docUrl(documentId)}`;
  const own = shares.shares[0];
  // Same derivation as the Access panel: published = an own row OR an
  // ancestor's subtree share covering this doc (summary 'inherited').
  const view = deriveShareState(shares.shares, shares.inherited_from);
  const published = view.shared || view.summary === 'inherited';

  const run = async (action: () => Promise<unknown>, copyAfter: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      await action();
      if (copyAfter) copyWithToast(link, showToast);
    } catch {
      showToast(t('artifactPublishFailed'), 'error');
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  if (!published) {
    return (
      <Button variant="ghost" size="sm" disabled={busy}
        onClick={() => run(() => createShare(projectId, documentId, 'doc'), true)}>
        <Globe size={13} />
        {t('artifactPublish')}
      </Button>
    );
  }
  return (
    <div className="flex items-center gap-1 text-ui-sm text-text-dim">
      <Globe size={13} className="text-amber" />
      <span>{t('artifactPublished')}</span>
      <span>·</span>
      <Button variant="ghost" size="sm" onClick={() => copyWithToast(link, showToast)}>{t('artifactCopyLink')}</Button>
      {/* A link inherited from an ancestor's subtree share is revoked there, not here. */}
      {own && (
        <>
          <span>·</span>
          <Button variant="ghost" size="sm" danger disabled={busy}
            onClick={() => run(() => revokeShare(own.share_id), false)}>
            {t('artifactRevoke')}
          </Button>
        </>
      )}
    </div>
  );
}
