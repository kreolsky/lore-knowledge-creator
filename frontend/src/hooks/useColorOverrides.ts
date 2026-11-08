/** Keeps the root element's inline color overrides in sync with the active theme, the user's prefs and the public-share rule; mirrors them to the pre-paint cache. */
import { useEffect } from 'react';
import { useUIStore } from '../store/ui-store';
import { useAppStore } from '../store/app-store';
import { registerLogoutHandler } from '../store/logout-handlers';
import { applyColorOverrides, cacheColors, clearCachedColors } from '../theme/apply-colors';
import { emptyOverrides } from '../theme/color-tokens';

// INVARIANT(security): logout drops the cached and the applied overrides.
// Why: another account on the same browser must not inherit (or see) the previous user's palette.
registerLogoutHandler(() => {
  clearCachedColors();
  useUIStore.setState({ colorOverrides: emptyOverrides() });
});

export function useColorOverrides(): void {
  const theme = useUIStore(s => s.theme);
  const overrides = useUIStore(s => s.colorOverrides);
  const isPublicShare = useUIStore(s => s.isPublicShare);
  const loaded = useUIStore(s => s.globalPrefsLoaded);
  const flushColorSave = useUIStore(s => s.flushColorSave);
  const userId = useAppStore(s => s.currentUser?.user_id ?? null);

  useEffect(() => {
    // WHY: the public published view always shows the default palette, for guests
    // and signed-in viewers alike — one rule.
    // Why: a share page is the author's document, not the viewer's workspace.
    applyColorOverrides(theme, isPublicShare ? emptyOverrides() : overrides);
  }, [theme, overrides, isPublicShare]);

  useEffect(() => {
    if (!loaded || !userId || isPublicShare) return;
    cacheColors(userId, overrides);
  }, [loaded, userId, isPublicShare, overrides]);

  // A pick made right before closing the tab still reaches the server.
  useEffect(() => {
    window.addEventListener('beforeunload', flushColorSave);
    return () => window.removeEventListener('beforeunload', flushColorSave);
  }, [flushColorSave]);
}
