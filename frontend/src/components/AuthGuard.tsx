/** Auth guard — bootstraps an ALREADY-verified session: seeds the user, loads global prefs, keeps the session cookie alive. Store: setCurrentUser, loadGlobalPrefs, showToast. */
import type React from 'react';
import { useEffect, useState } from 'react';
import { apiClient } from '../api/client';
import type { User } from '../types';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { startSessionRefresh } from '../hooks/useSessionRefresh';
import { t } from '../i18n';

interface Props {
  /** The verified session user, from SessionRoute's probe of /auth/me.
   *  INVARIANT: the guard never verifies the session itself — mounting it means
   *  the session is already proven. Why: the probe's 200 body IS /auth/me's user,
   *  so a fetch here would be a second, serial round-trip on every cold load. The
   *  prop is required precisely so it cannot be mounted where the session is
   *  unknown. */
  user: User;
  children: React.ReactNode;
}

export function AuthGuard({ user, children }: Props) {
  const setCurrentUser = useAppStore(s => s.setCurrentUser);
  // No optimistic start (part C's seed, deleted): the guard used to seed `checking`
  // from the in-memory app-store because remounting the shell on a cross-branch
  // route change (/projects/:id ↔ /docs/:id) re-ran it and returned null for a
  // frame. SessionRoute now keeps this guard mounted across crossings, so the seed
  // had nothing left to soften. Children still wait for prefs so the theme never
  // flashes unstyled — that gate is what `checking` is now for.
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let stopRefresh: (() => void) | undefined;
    // WHY: guard against unmount-while-prefs-in-flight — without this, if the
    // component unmounts while loadGlobalPrefs is pending, cleanup runs with
    // stopRefresh still undefined and the scheduler started afterwards would leak
    // (interval + listener).
    let cancelled = false;
    setCurrentUser(user);
    void (async () => {
      await useUIStore.getState().loadGlobalPrefs();
      if (cancelled) return;
      // ARCH: client owns timezone detection — browsers expose IANA tz directly via
      // Intl.DateTimeFormat, while server-side guessing (Accept-Language, IP geoip)
      // is unreliable. Synced once per session if it drifts from the stored value.
      const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (detected && user.timezone !== detected) {
        // WHY: timezone sync is non-critical background telemetry — failure must not block session initialization
        apiClient.put(`/users/${user.user_id}`, { timezone: detected }).catch((e) => {
          console.warn('[AuthGuard] failed to sync timezone:', e);
        });
      }
      // WHY: start the sliding-window refresh only after a confirmed session —
      // polling /me before auth would just trigger a 401 redirect loop. The scheduler
      // re-issues the cookie every 6h and on tab regain so long-lived tabs never hit
      // the hard 14-day cap. t is a stable module-level import (not a hook), so it
      // is safe to reference inside the callback without listing it as an effect dep.
      stopRefresh = startSessionRefresh({
        // WHY: honest only while SessionRoute's /auth/me probe runs per mount of this
        // guard — the flag claims the window was JUST slid. If that probe is ever
        // memoized or hoisted across mounts, the scheduler starts on a stale window
        // and the session silently loses its first slide.
        skipInitialRefresh: true,
        onTransientFailure: () => useAppStore.getState().showToast(t('serverUnavailable')),
      });
      setChecking(false);
    })();
    return () => {
      cancelled = true;
      stopRefresh?.();
    };
  }, [setCurrentUser, user]);

  if (checking) return null;
  return <>{children}</>;
}
