// Applies the cached theme class before first paint, so a reload in dark theme never flashes light.
// WHY: an external classic script, not inline — prod CSP is `script-src 'self'`.
// The key is written by the theme slice (THEME_CACHE_KEY in store/ui-store/theme-slice.ts);
// the server prefs stay the source of truth and overwrite it once loaded.
try {
  if (localStorage.getItem('lore.theme') === 'dark') document.documentElement.classList.add('dark');
} catch {
  // Storage blocked: first paint is light, server prefs apply the theme after load.
}
