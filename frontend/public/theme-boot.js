// Applies the cached theme class and the user's cached color overrides before first paint,
// so a reload never flashes light or the default palette.
// WHY: an external classic script, not inline — prod CSP is `script-src 'self'`.
// Keys are written by the app: 'lore.theme' = THEME_CACHE_KEY (store/ui-store/theme-slice.ts),
// 'lore.colors' = COLORS_CACHE_KEY (theme/apply-colors.ts, which also filters the values).
// The server prefs stay the source of truth and overwrite both once loaded.
try {
  var dark = localStorage.getItem('lore.theme') === 'dark';
  if (dark) document.documentElement.classList.add('dark');
  // Public share links (/s/…) always paint the default palette (see useColorOverrides).
  if (location.pathname.indexOf('/s/') !== 0) {
    var cached = JSON.parse(localStorage.getItem('lore.colors') || 'null');
    var map = cached && cached.applied && cached.applied[dark ? 'dark' : 'light'];
    if (map && typeof map === 'object') {
      for (var k in map) {
        var v = map[k];
        // Values were filtered by applyColorOverrides' sanitizer before caching; derived
        // shades legitimately carry color-mix(var(…)), so only url() is refused here.
        if (k.indexOf('--') === 0 && typeof v === 'string' && !/url\(/i.test(v)) {
          document.documentElement.style.setProperty(k, v);
        }
      }
    }
  }
} catch {
  // Storage blocked or cache corrupt: first paint uses defaults, server prefs apply after load.
}
