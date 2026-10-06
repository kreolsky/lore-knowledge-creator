/** Layout — the authed app frame: chromeless editor shell or Header + NavigationTabBar.
 *
 * Moved out of App.tsx by plan "route-unification-single-shell" so that
 * SessionRoute's authed branch owns it — which is what finally routes /docs/:id
 * THROUGH this component (PIN gate + shell selection) instead of around it.
 */

import type React from 'react';
import { useLocation } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { PinLockScreen } from './PinLockScreen';
import { NavigationTabBar } from './NavigationTabBar';
import { Header } from './Header';
import { isEditorShell } from '../utils/routing';

export function Layout({ children }: { children: React.ReactNode }) {
  const pinLocked = useAppStore(s => s.pinLocked);
  const location = useLocation();
  // /docs/<id> renders in the SAME chromeless shell as /projects/<id> — the
  // editor shell is selected by URL shape via one predicate (plan
  // "public-document-ids"), not the legacy /projects/ regex.
  const inEditorShell = isEditorShell(location.pathname);

  // INVARIANT(security): the lock REPLACES the tree (never overlays) and covers
  // /docs/:id — the canonical editor URL. Why: /docs/:id renders through this
  // Layout only since plan "route-unification-single-shell"; before that it was a
  // top-level sibling that bypassed this gate entirely, so a locked member's
  // document stayed reachable at its canonical URL (lessons/2026-03-20-security
  // -review-pin-lock.md). Moving /docs/:id out from under this gate reopens that
  // hole — Layout.test.tsx pins it.
  if (pinLocked) return <PinLockScreen />;
  if (inEditorShell) {
    return (
      <div className="h-dvh bg-bg text-text">
        <main className="h-full">{children}</main>
      </div>
    );
  }

  // Section pages mount their own SectionShell (left tab bar + aside + back
  // button) — rendering NavigationTabBar beside it would double the chrome.
  const isSectionShell = location.pathname === '/admin' || location.pathname === '/cabinet';

  return (
    <div className="flex flex-col h-dvh bg-bg text-text">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        {!isSectionShell && <NavigationTabBar />}
        <main className="flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}
