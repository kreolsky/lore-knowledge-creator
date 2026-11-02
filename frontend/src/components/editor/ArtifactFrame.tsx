/**
 * ArtifactFrame — an html page (media_type 'html') in a sandboxed iframe served
 * from the separate artifact origin (see SYSTEM: artifact-serve, backend).
 *
 * The frame never receives the page source: it mints a short-lived signed URL
 * through `fetchUrl` (member or public endpoint) and points the iframe at it.
 * A content change (the owner bumps `changeSeq`) reloads with a fresh URL after
 * RELOAD_DEBOUNCE_MS. Token expiry re-mints the URL WITHOUT reloading the live
 * page: the re-mint is the access re-check (revocation / removal drops the frame
 * into the error state), while a successful one only replaces the URL the next
 * reload uses — a page keeps its own state between edits.
 *
 * Error states are distinct from loading and from each other: hosting not
 * configured (ARTIFACT_ORIGIN unset), load failed, and navigated (the guard).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle } from 'lucide-react';
import type * as Y from 'yjs';
import { HttpError } from '../../api/client';
import { Button } from '../ui';
import { useTranslation } from '../../i18n';

export interface ArtifactUrl {
  url: string;
  expires_in: number;
}

type FrameState =
  | { kind: 'loading' }
  | { kind: 'ready'; url: string }
  | { kind: 'error'; reason: 'unconfigured' | 'failed' | 'navigated' };

const RELOAD_DEBOUNCE_MS = 1000;
// Re-mint this long before the token lapses, so the check lands while it is still valid.
const EXPIRY_MARGIN_S = 30;

function isUnconfigured(err: unknown): boolean {
  return err instanceof HttpError && err.status === 422 && err.detail.includes('not configured');
}

interface ArtifactFrameProps {
  /** Mints a signed URL. Must be referentially stable per document (useCallback). */
  fetchUrl: () => Promise<ArtifactUrl>;
  /** Bumped on every content change; each bump reloads the page after the debounce. */
  changeSeq: number;
}

export function ArtifactFrame({ fetchUrl, changeSeq }: ArtifactFrameProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<FrameState>({ kind: 'loading' });
  const [reloadSeq, setReloadSeq] = useState(0);
  const navigatedRef = useRef(false);
  // Load events seen by the CURRENT iframe element, keyed by its URL (the element is
  // keyed by URL too, so a late event from a replaced element never counts here).
  const loadsRef = useRef<{ url: string; count: number }>({ url: '', count: 0 });

  const fail = useCallback((err: unknown) => {
    console.warn('artifact url mint failed', err);
    setState({ kind: 'error', reason: isUnconfigured(err) ? 'unconfigured' : 'failed' });
  }, []);

  // Mount: mint + show. Re-runs on a manual reload or a debounced content change.
  useEffect(() => {
    let cancelled = false;
    let expiryTimer: ReturnType<typeof setTimeout> | undefined;
    const scheduleRecheck = (expiresIn: number) => {
      expiryTimer = setTimeout(() => {
        fetchUrl().then(
          next => { if (!cancelled) scheduleRecheck(next.expires_in); },
          err => { if (!cancelled) fail(err); },
        );
      }, Math.max(expiresIn - EXPIRY_MARGIN_S, 5) * 1000);
    };
    fetchUrl().then(
      ({ url, expires_in }) => {
        if (cancelled || navigatedRef.current) return;
        loadsRef.current = { url, count: 0 };
        setState({ kind: 'ready', url });
        scheduleRecheck(expires_in);
      },
      err => { if (!cancelled) fail(err); },
    );
    return () => {
      cancelled = true;
      clearTimeout(expiryTimer);
    };
  }, [fetchUrl, reloadSeq, fail]);

  // Content change → reload after the debounce (never out of the navigated state).
  const firstSeqRef = useRef(changeSeq);
  useEffect(() => {
    if (changeSeq === firstSeqRef.current) return;
    const timer = setTimeout(() => {
      if (!navigatedRef.current) setReloadSeq(s => s + 1);
    }, RELOAD_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [changeSeq]);

  // INVARIANT(security): any load of the iframe after its first one drops the frame.
  // Why: nothing (no CSP directive, no sandbox flag) stops a frame navigating itself,
  // so a foreign or injected page could draw a fake Lore login inside the window and
  // send the input away. The served page loads exactly once per URL; a second load is
  // a navigation (or reload) the page did on its own.
  const handleLoad = (url: string) => {
    if (loadsRef.current.url !== url) return;
    loadsRef.current.count += 1;
    if (loadsRef.current.count > 1) {
      navigatedRef.current = true;
      setState({ kind: 'error', reason: 'navigated' });
    }
  };

  const reload = () => {
    navigatedRef.current = false;
    setState({ kind: 'loading' });
    setReloadSeq(s => s + 1);
  };

  return (
    <div className="relative h-full w-full min-h-0">
          {state.kind === 'ready' && (
            <iframe
              key={state.url}
              src={state.url}
              title={t('artifactUserPage')}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              className="absolute inset-0 w-full h-full border-0"
              onLoad={() => handleLoad(state.url)}
            />
          )}
          {state.kind === 'loading' && (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="w-6 h-6 border-2 border-text-dim border-t-transparent animate-spin" />
            </div>
          )}
          {state.kind === 'error' && (
            <div className="absolute inset-0 flex items-center justify-center px-4" data-artifact-error={state.reason}>
              <div className="flex flex-col items-center gap-3 max-w-md text-center">
                <AlertCircle size={24} className={state.reason === 'navigated' ? 'text-red' : 'text-text-dim'} />
                <p className="text-ui-base text-text">
                  {t(state.reason === 'unconfigured' ? 'artifactNotConfigured'
                    : state.reason === 'navigated' ? 'artifactNavigated' : 'artifactLoadFailed')}
                </p>
                {state.reason !== 'unconfigured' && (
                  <Button variant="ghost" size="sm" onClick={reload}>{t('artifactReload')}</Button>
                )}
              </div>
            </div>
          )}
    </div>
  );
}

/** A counter bumped on every change of `ytext` while it is non-null — the
 *  member-side `changeSeq` source (the guest's is the public change-ping). */
export function useYtextChangeSeq(ytext: Y.Text | null): number {
  const [seq, setSeq] = useState(0);
  useEffect(() => {
    if (!ytext) return;
    const onChange = () => setSeq(s => s + 1);
    ytext.observe(onChange);
    return () => ytext.unobserve(onChange);
  }, [ytext]);
  return seq;
}
