/** Curated registry of user-recolorable theme tokens — the one source for the cabinet Colors UI, the backup import allowlist and the DOM application.
 *
 * WHY: entries carry names and i18n keys only, never color literals — the defaults live in
 * `index.css` (:root / html.dark), so a later palette change reaches every token the user
 * did not touch. Every token here is opaque, so a hex-only native color input suffices.
 */
import type { TranslationKey } from '../i18n';

export type ThemeName = 'light' | 'dark';
export type ColorMap = Record<string, string>;
export interface ColorOverrides { light: ColorMap; dark: ColorMap }

export type ColorGroup = 'background' | 'text' | 'accent' | 'status' | 'highlights';

export interface ColorToken {
  token: string;
  group: ColorGroup;
  label: TranslationKey;
  /** Shades recomputed from this token while (and only while) it is overridden, per theme.
   * WHY: an untouched theme stays pixel-identical to its hand-tuned CSS. */
  derived?: Partial<Record<ThemeName, ColorMap>>;
}

const mix = (token: string, pct: number, other: string) =>
  `color-mix(in srgb, var(${token}) ${pct}%, ${other})`;

export const COLOR_TOKENS: readonly ColorToken[] = [
  { token: '--bg', group: 'background', label: 'colorTokenBg' },
  { token: '--surface', group: 'background', label: 'colorTokenSurface' },
  { token: '--surface2', group: 'background', label: 'colorTokenSurface2' },
  { token: '--surface3', group: 'background', label: 'colorTokenSurface3' },
  { token: '--border', group: 'background', label: 'colorTokenBorder' },
  { token: '--header-bg', group: 'background', label: 'colorTokenHeaderBg' },
  { token: '--text', group: 'text', label: 'colorTokenText' },
  { token: '--text-muted', group: 'text', label: 'colorTokenTextMuted' },
  { token: '--text-dim', group: 'text', label: 'colorTokenTextDim' },
  { token: '--text-strong', group: 'text', label: 'colorTokenTextStrong' },
  {
    token: '--accent', group: 'accent', label: 'colorTokenAccent',
    // Dark links/active icons use --accent-text (a lighter hue); light aliases it to --accent.
    derived: { dark: { '--accent-text': mix('--accent', 65, 'white') } },
  },
  { token: '--accent-text', group: 'accent', label: 'colorTokenAccentText' },
  { token: '--green', group: 'status', label: 'colorTokenGreen' },
  { token: '--amber', group: 'status', label: 'colorTokenAmber' },
  { token: '--red', group: 'status', label: 'colorTokenRed' },
  { token: '--blue', group: 'status', label: 'colorTokenBlue' },
  { token: '--orange', group: 'status', label: 'colorTokenOrange' },
  {
    token: '--warm-strong', group: 'highlights', label: 'colorTokenWarmStrong',
    // The dark warm family is neutral by design: recoloring strong recolors only strong.
    derived: {
      light: {
        '--warm-tint': mix('--warm-strong', 55, 'white'),
        '--warm-mid': mix('--warm-strong', 75, 'white'),
        '--warm-sep': mix('--warm-strong', 75, 'var(--text-dim)'),
        '--warm-ink': mix('--warm-strong', 40, 'black'),
      },
    },
  },
  {
    token: '--ref-solid', group: 'highlights', label: 'colorTokenRefSolid',
    derived: {
      light: { '--ref-tint': mix('--ref-solid', 35, 'white'), '--ref-strong': mix('--ref-solid', 55, 'white') },
      dark: { '--ref-tint': mix('--ref-solid', 25, 'transparent'), '--ref-strong': mix('--ref-solid', 70, 'var(--bg)') },
    },
  },
  {
    token: '--snapshot-solid', group: 'highlights', label: 'colorTokenSnapshotSolid',
    derived: {
      light: { '--snapshot-tint': mix('--snapshot-solid', 30, 'white'), '--snapshot-strong': mix('--snapshot-solid', 65, 'white') },
      dark: { '--snapshot-tint': mix('--snapshot-solid', 25, 'transparent'), '--snapshot-strong': mix('--snapshot-solid', 70, 'var(--bg)') },
    },
  },
];

export const COLOR_GROUPS: readonly { group: ColorGroup; label: TranslationKey }[] = [
  { group: 'background', label: 'colorGroupBackground' },
  { group: 'text', label: 'colorGroupText' },
  { group: 'accent', label: 'colorGroupAccent' },
  { group: 'status', label: 'colorGroupStatus' },
  { group: 'highlights', label: 'colorGroupHighlights' },
];

const REGISTRY_KEYS = new Set(COLOR_TOKENS.map(t => t.token));

/** Every CSS property the overrides may set — registry tokens plus every derived shade. */
export const ALL_MANAGED_PROPERTIES: readonly string[] = [...new Set([
  ...REGISTRY_KEYS,
  ...COLOR_TOKENS.flatMap(t => Object.values(t.derived ?? {}).flatMap(m => Object.keys(m))),
])];

/** A value the user may set. INVARIANT(security): `var(`, `calc(` and `url(` are refused.
 * Why: CSS.supports accepts any var()/calc() at parse time, and url() would let an
 * imported backup or a tampered prefs blob (the server does not validate it) make requests. */
export function isSafeColor(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 64) return false;
  if (/var\(|calc\(|url\(/i.test(value)) return false;
  return typeof CSS !== 'undefined' && CSS.supports('color', value);
}

/** Keep only registry keys with safe values; report how many entries were dropped. */
export function sanitizeColorMap(raw: unknown): { map: ColorMap; dropped: number } {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { map: {}, dropped: 0 };
  const map: ColorMap = {};
  let dropped = 0;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (REGISTRY_KEYS.has(k) && isSafeColor(v)) map[k] = v;
    else dropped += 1;
  }
  return { map, dropped };
}

export function emptyOverrides(): ColorOverrides {
  return { light: {}, dark: {} };
}

/** Normalize a raw (server/localStorage) value into a clean overrides object. */
export function sanitizeOverrides(raw: unknown): ColorOverrides {
  const obj = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  return { light: sanitizeColorMap(obj.light).map, dark: sanitizeColorMap(obj.dark).map };
}
