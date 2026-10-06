/**
 * TokenRedirect — legacy /s/:token → /docs/:id (plan "public-document-ids").
 *
 * The canonical URL is now /docs/<document_id>. A visitor holding a legacy
 * /s/:token link is forwarded: resolve the token to its share-root document_id
 * (the legacy /public/{token}/tree endpoint still returns root_id), then
 * <Navigate replace> to /docs/<root_id>. From there SessionRoute takes over
 * (anonymous → PublicSharePage document-keyed). The redirect is a one-shot
 * resolver — once the ecosystem's links are regenerated to /docs/<id> (the mint
 * response already returns that URL), this path is rarely hit.
 *
 * INVARIANT: a bad/expired token surfaces the public not-found state (no blank,
 * no redirect loop). Why: publicTree 404s and we render NotFound — a uniform
 * anonymous 404 with no existence oracle, mirroring the public-share surface.
 * The token never appears in the destination URL.
 */

import { useEffect, useState } from 'react';
import { useParams, Navigate, useNavigate } from 'react-router-dom';
import { publicTree } from '../api/public-share';
import { docUrl } from '../utils/routing';
import { useTranslation } from '../i18n';
import { Button } from '../components/ui';

function RouteLoading() {
  return (
    <div className="flex h-dvh items-center justify-center bg-bg text-text">
      <div className="w-6 h-6 border-2 border-text-dim border-t-transparent animate-spin" />
    </div>
  );
}

function NotFound() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  return (
    <div className="h-dvh flex flex-col items-center justify-center gap-4 bg-bg text-text">
      <h1 className="text-lg font-medium text-text-dim">{t('publicShareNotFound')}</h1>
      <Button variant="primary" onClick={() => navigate('/')}>{t('backToHome')}</Button>
    </div>
  );
}

export function TokenRedirect() {
  const { token = '' } = useParams<{ token: string }>();
  const [rootId, setRootId] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    publicTree(token)
      .then((tree) => { if (!cancelled) setRootId(tree.root_id); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [token]);

  if (failed) return <NotFound />;
  if (rootId) return <Navigate replace to={docUrl(rootId)} />;
  return <RouteLoading />;
}
