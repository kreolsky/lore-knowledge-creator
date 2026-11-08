/** Applies per-theme user color overrides to the root element, caches them for theme-boot.js, and parses the JSON backup.
 *
 * ARCH: overrides are inline custom properties on <html>: inline style beats both `:root`
 * and `html.dark`, and JS readers of getComputedStyle see the overridden value.
 */
import {
  ALL_MANAGED_PROPERTIES, COLOR_TOKENS, sanitizeColorMap, sanitizeOverrides,
  type ColorOverrides, type ThemeName,
} from './color-tokens';

/** WHY: per-device mirror read by public/theme-boot.js before first paint (same key there).
 * Shape `{ uid, overrides, applied }`; removed on logout so another account never inherits it. */
export const COLORS_CACHE_KEY = 'lore.colors';

/** Strip every managed property, then set derived shades first and explicit overrides after.
 * INVARIANT(security): values are filtered here, not trusted from the caller.
 * Why: loadGlobalPrefs loads raw and the server does not validate the prefs blob. */
export function applyColorOverrides(theme: ThemeName, overrides: ColorOverrides): void {
  const style = document.documentElement.style;
  for (const prop of ALL_MANAGED_PROPERTIES) style.removeProperty(prop);
  for (const [prop, value] of Object.entries(resolveProperties(theme, overrides))) style.setProperty(prop, value);
}

/** The full property map one theme's overrides put on <html>, derived shades included.
 * Precedence: derived first, so a user-set --accent-text beats the accent's derived one. */
function resolveProperties(theme: ThemeName, overrides: ColorOverrides): Record<string, string> {
  const { map } = sanitizeColorMap(overrides[theme]);
  const props: Record<string, string> = {};
  for (const entry of COLOR_TOKENS) {
    if (entry.token in map) Object.assign(props, entry.derived?.[theme] ?? {});
  }
  return Object.assign(props, map);
}

export function cacheColors(uid: string, overrides: ColorOverrides): void {
  try {
    const empty = !Object.keys(overrides.light).length && !Object.keys(overrides.dark).length;
    if (empty) localStorage.removeItem(COLORS_CACHE_KEY);
    // `applied` is what theme-boot.js paints (derived shades included); `overrides` seeds the store.
    else localStorage.setItem(COLORS_CACHE_KEY, JSON.stringify({
      uid,
      overrides,
      applied: { light: resolveProperties('light', overrides), dark: resolveProperties('dark', overrides) },
    }));
  } catch {
    // Storage blocked: only the pre-paint shortcut is lost; overrides apply after prefs load.
  }
}

/** The cached overrides theme-boot.js painted, so the first React apply keeps them
 * while server prefs are still loading. */
export function readCachedColors(): ColorOverrides {
  try {
    return sanitizeOverrides(JSON.parse(localStorage.getItem(COLORS_CACHE_KEY) ?? '{}')?.overrides);
  } catch {
    return { light: {}, dark: {} };
  }
}

export function clearCachedColors(): void {
  try { localStorage.removeItem(COLORS_CACHE_KEY); } catch { /* storage blocked: nothing cached */ }
}

/** The CSS default of `token` in the active theme: drop the inline override, read, restore.
 * WHY: synchronous, so no paint happens between — no second table of defaults. */
export function readDefaultColor(token: string): string {
  const style = document.documentElement.style;
  const current = style.getPropertyValue(token);
  if (current) style.removeProperty(token);
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  if (current) style.setProperty(token, current);
  return value;
}

export const BACKUP_FORMAT = 'lore-colors';
export const BACKUP_VERSION = 1;

export function buildColorBackup(overrides: ColorOverrides): string {
  return JSON.stringify({ format: BACKUP_FORMAT, version: BACKUP_VERSION, colors: sanitizeOverrides(overrides) }, null, 2);
}

export type ColorBackupResult =
  | { ok: true; colors: ColorOverrides; dropped: { light: number; dark: number } }
  | { ok: false; error: 'invalidJson' | 'wrongFormat' | 'unsupportedVersion' };

/** One validator for an imported backup: wrong format/version rejects the whole file;
 * unknown keys and unsafe values are dropped and counted, never applied. */
export function parseColorBackup(text: string): ColorBackupResult {
  let data: unknown;
  try { data = JSON.parse(text); } catch { return { ok: false, error: 'invalidJson' }; }
  const obj = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>;
  if (obj.format !== BACKUP_FORMAT || !obj.colors || typeof obj.colors !== 'object') {
    return { ok: false, error: 'wrongFormat' };
  }
  if (obj.version !== BACKUP_VERSION) return { ok: false, error: 'unsupportedVersion' };
  const colors = obj.colors as Record<string, unknown>;
  const light = sanitizeColorMap(colors.light);
  const dark = sanitizeColorMap(colors.dark);
  return { ok: true, colors: { light: light.map, dark: dark.map }, dropped: { light: light.dropped, dark: dark.dropped } };
}
