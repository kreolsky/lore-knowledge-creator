/** Global-prefs slice — theme, language, panel widths, split ratio (global-persisted).

# ARCH (debt-paydown W6): fourth ui-store slice. Owns the global-prefs concern end-to-end
# — state + setters + loadGlobalPrefs + the SPLIT_RATIO clamp consts + GLOBAL_DEFAULTS +
# the _globalPrefsPromise dedup singleton — so it imports only apiClient + types and has no
# circular dep with ui-store's persistence core. Setters persist via the store method
# get().saveGlobalPrefsNow() (added to ui-store alongside saveUIStateNow); the
# incident-sensitive triggerSaveGlobalPrefs stays untouched in ui-store.ts.
*/
import { apiClient } from '../../api/client';
import type { Language } from '../../types';
import type { UIState } from '../ui-store';
import { readCachedColors } from '../../theme/apply-colors';
import { emptyOverrides, sanitizeOverrides, type ColorOverrides } from '../../theme/color-tokens';
import { DEFAULT_SOUND_PRESET, isSoundChoice, type SoundChoice } from '../../utils/sound-presets';

export type ThemeSlice = Pick<
  UIState,
  | 'theme' | 'colorOverrides' | 'language' | 'panelWidths' | 'splitRatio' | 'globalPrefsLoaded'
  | 'showArchived' | 'adminSectionTab' | 'cabinetSectionTab' | 'sidebarPinned'
  | 'setTheme' | 'setLanguage' | 'setPanelWidth' | 'setSplitRatio' | 'getSplitRatio' | 'loadGlobalPrefs'
  | 'setShowArchived' | 'setAdminSectionTab' | 'setCabinetSectionTab' | 'setSidebarPinned'
  | 'setColorOverride' | 'replaceColorOverrides' | 'flushColorSave'
  | 'soundPreset' | 'setSoundPreset'
>;

type UISet = (
  partial: Partial<UIState> | ((state: UIState) => Partial<UIState>),
) => void;
type UIGet = () => UIState;

export const SPLIT_RATIO_DEFAULT = 0.5;
export const SPLIT_RATIO_MIN = 0.2;
export const SPLIT_RATIO_MAX = 0.8;

/** The admin panel's left-aside sections. Lives here (not in AdminPage) so the
 * persisted GlobalPrefs field and the page share one union — the store stays
 * free of page imports. The settings ids are `settings:<registry tab>` — the
 * LIST is fixed in TSX (a new tab needs an i18n label and an icon anyway, and
 * the saved-section validation at mount runs before any fetch); the registry
 * is only the source of the KEYS inside a tab. */
export type AdminSectionTab =
  | 'info'
  | 'users'
  | 'projects'
  | 'embeddings'
  | 'settings:models'
  | 'settings:agent'
  | 'settings:tools'
  | 'settings:search'
  | 'settings:storage'
  | 'skills'
  | 'model-access'
  | 'groups'
  | 'mcp-servers';

/** The cabinet's left-aside sections, same placement reason as AdminSectionTab. */
export type CabinetSectionTab = 'profile' | 'mcp' | 'keys' | 'colors' | 'sound';

export interface GlobalPrefs {
  theme: 'light' | 'dark';
  panelWidths: { left: number | null; right: number | null };
  language: Language;
  // ARCH: split-column width ratio (left column fraction, 0–1) is a global layout
  // preference like panelWidths — not per-document. Clamped to [0.2, 0.8].
  splitRatio: number;
  // ARCH: the "Show archived" reference-panel filter is a
  // global VIEWING preference (not a per-doc layout), so it persists on _global like
  // theme/language. OFF → default LIST (archived hidden); ON → include_archived=true.
  showArchived: boolean;
  // ARCH: the admin panel's last-open section, persisted per user on _global —
  // never in a project's prefs blob (SectionShell's standing invariant). The
  // store saves it raw; the page validates it at mount against the sections the
  // user is offered (a moderator has no Embeddings row).
  adminSectionTab: AdminSectionTab | null;
  // Same contract as adminSectionTab, for the cabinet's last-open section.
  cabinetSectionTab: CabinetSectionTab | null;
  // WHY: global like panelWidths — docked vs auto-hiding left panel is a layout habit.
  sidebarPinned: boolean;
  // INVARIANT(persisted): sparse per-theme map token → color; reset = key deleted.
  // Why: defaults stay in index.css, so a later palette change reaches every token
  // the user did not touch. Loaded raw-then-sanitized (the server does not validate).
  colorOverrides: ColorOverrides;
  // WHY: global like theme — which chime a person hears is a habit, not a project fact;
  // only the on/off toggle is per project. Loaded raw-then-validated (unknown id → default).
  soundPreset: SoundChoice;
}

const GLOBAL_DEFAULTS: GlobalPrefs = {
  theme: 'light',
  panelWidths: { left: null, right: null },
  language: 'en',
  splitRatio: SPLIT_RATIO_DEFAULT,
  showArchived: false,
  adminSectionTab: null,
  cabinetSectionTab: null,
  sidebarPinned: true,
  colorOverrides: { light: {}, dark: {} },
  soundPreset: DEFAULT_SOUND_PRESET,
};

// WHY: a color picker fires per drag tick; the save is debounced while the apply is instant.
const COLOR_SAVE_DEBOUNCE_MS = 400;
let _colorSaveTimer: ReturnType<typeof setTimeout> | null = null;

// WHY: per-device mirror of the server theme, read by public/theme-boot.js before first
// paint so a dark reload does not flash light. Server prefs remain the source of truth.
const THEME_CACHE_KEY = 'lore.theme';

function readCachedTheme(): GlobalPrefs['theme'] {
  try {
    return localStorage.getItem(THEME_CACHE_KEY) === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

function cacheTheme(theme: GlobalPrefs['theme']): void {
  try {
    localStorage.setItem(THEME_CACHE_KEY, theme);
  } catch {
    // Storage blocked: only the pre-paint shortcut is lost, the theme itself still applies.
  }
}

// In-flight loadGlobalPrefs promise — dedup gate (AuthGuard's mount effect runs twice
// under StrictMode, doubling GET /api/preferences/_global).
let _globalPrefsPromise: Promise<void> | null = null;

export function createThemeSlice(set: UISet, get: UIGet): ThemeSlice {
  return {
    // WHY: start from the cache so useTheme's mount effect does not strip the class
    // theme-boot.js already applied while server prefs are still loading.
    theme: readCachedTheme(),
    language: 'en',
    panelWidths: { left: null, right: null },
    splitRatio: SPLIT_RATIO_DEFAULT,
    showArchived: false,
    adminSectionTab: null,
    cabinetSectionTab: null,
    sidebarPinned: true,
    // WHY: same reason as theme above — keep what theme-boot.js painted until prefs load.
    colorOverrides: readCachedColors(),
    soundPreset: DEFAULT_SOUND_PRESET,
    globalPrefsLoaded: false,    setTheme: (theme) => { set({ theme }); cacheTheme(theme); get().saveGlobalPrefsNow(); },
    setLanguage: (language) => { set({ language }); get().saveGlobalPrefsNow(); },
    setPanelWidth: (side, width) => {
      set(prev => ({ panelWidths: { ...prev.panelWidths, [side]: width } }));
      get().saveGlobalPrefsNow();
    },
    setSplitRatio: (ratio) => {
      const clamped = Math.min(SPLIT_RATIO_MAX, Math.max(SPLIT_RATIO_MIN, ratio));
      set({ splitRatio: clamped });
      get().saveGlobalPrefsNow();
    },
    getSplitRatio: () => get().splitRatio,
    // Persists on _global (setShowArchived → saveGlobalPrefsNow).
    setShowArchived: (showArchived) => { set({ showArchived }); get().saveGlobalPrefsNow(); },
    // Admin section pref — same persistence route as showArchived (per-user _global).
    setAdminSectionTab: (tab) => { set({ adminSectionTab: tab }); get().saveGlobalPrefsNow(); },
    setCabinetSectionTab: (tab) => { set({ cabinetSectionTab: tab }); get().saveGlobalPrefsNow(); },
    setSidebarPinned: (sidebarPinned) => { set({ sidebarPinned }); get().saveGlobalPrefsNow(); },
    setSoundPreset: (soundPreset) => { set({ soundPreset }); get().saveGlobalPrefsNow(); },
    setColorOverride: (theme, token, value) => {
      set(prev => {
        const map = { ...prev.colorOverrides[theme] };
        if (value === null) delete map[token];
        else map[token] = value;
        return { colorOverrides: { ...prev.colorOverrides, [theme]: map } };
      });
      if (_colorSaveTimer) clearTimeout(_colorSaveTimer);
      _colorSaveTimer = setTimeout(() => { _colorSaveTimer = null; get().saveGlobalPrefsNow(); }, COLOR_SAVE_DEBOUNCE_MS);
    },
    replaceColorOverrides: (overrides) => {
      if (_colorSaveTimer) { clearTimeout(_colorSaveTimer); _colorSaveTimer = null; }
      set({ colorOverrides: sanitizeOverrides(overrides) });
      get().saveGlobalPrefsNow();
    },
    flushColorSave: () => {
      if (!_colorSaveTimer) return;
      clearTimeout(_colorSaveTimer);
      _colorSaveTimer = null;
      get().saveGlobalPrefsNow();
    },
    loadGlobalPrefs: () => {
      if (_globalPrefsPromise) return _globalPrefsPromise;
      _globalPrefsPromise = (async () => {
        try {
          const data: Partial<GlobalPrefs> = await apiClient.get('/preferences/_global');
          if (!data || Object.keys(data).length === 0) {
            // Empty prefs = a fresh account: drop any colors left by a previous one.
            set({ colorOverrides: emptyOverrides(), globalPrefsLoaded: true });
            return;
          }
          const theme = data.theme ?? GLOBAL_DEFAULTS.theme;
          cacheTheme(theme);
          set({
            theme,
            panelWidths: {
              left: data.panelWidths?.left ?? GLOBAL_DEFAULTS.panelWidths.left,
              right: data.panelWidths?.right ?? GLOBAL_DEFAULTS.panelWidths.right,
            },
            language: data.language ?? GLOBAL_DEFAULTS.language,
            splitRatio: data.splitRatio ?? GLOBAL_DEFAULTS.splitRatio,
            showArchived: data.showArchived ?? GLOBAL_DEFAULTS.showArchived,
            // Garbage-proofing is the PAGE's job (availability check at mount);
            // here unknown values load raw and never block hydration.
            adminSectionTab: data.adminSectionTab ?? null,
            cabinetSectionTab: data.cabinetSectionTab ?? null,
            sidebarPinned: data.sidebarPinned ?? GLOBAL_DEFAULTS.sidebarPinned,
            colorOverrides: sanitizeOverrides(data.colorOverrides),
            soundPreset: isSoundChoice(data.soundPreset) ? data.soundPreset : GLOBAL_DEFAULTS.soundPreset,
            globalPrefsLoaded: true,
          });
        } catch {
          set({ globalPrefsLoaded: true });
        } finally {
          // Null out so a later genuine reload (e.g. re-login) can refetch.
          _globalPrefsPromise = null;
        }
      })();
      return _globalPrefsPromise;
    },
  };
}
