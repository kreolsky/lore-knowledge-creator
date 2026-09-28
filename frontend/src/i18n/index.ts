/** Lightweight i18n: reactive hook for components, standalone t() for store/handlers. */
// ARCH: Custom i18n instead of i18next — ~40 lines vs ~40KB library for ~200 keys and 2 languages.
// Two access patterns: useTranslation() hook (reactive, for JSX) and standalone t() (imperative, for store/handlers).
// Fallback chain: current language → English → raw key. Type safety via TranslationKey derived from en.ts.
// SYSTEM: i18n — lightweight internationalization (EN/RU), reactive hook + imperative t()

import { useCallback } from 'react';
import { en, type TranslationKey } from './en';
import { ru } from './ru';
import { useUIStore } from '../store/ui-store';
import type { Language } from '../types';

const translations: Record<Language, Record<TranslationKey, string>> = { en, ru };

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

/** Platform-aware modifier symbols, auto-injected into {mod}, {shift}, {alt} placeholders. */
const modifiers: Record<string, string> = {
  mod: isMac ? '\u2318' : 'Ctrl+',
  shift: isMac ? '\u21E7' : 'Shift+',
  alt: isMac ? '\u2325' : 'Alt+',
};

function interpolate(template: unknown, vars?: Record<string, string | number>): string {
  // INVARIANT: a bad key degrades to visible text, never to a thrown render.
  // Why: the last fallback of the chain below is the KEY itself, so a lookup
  // that produced no key at all (a map arm missing for a value the backend
  // widened — an unknown halt reason did exactly this) reached `.replace` on
  // undefined and took the whole chat down through the error boundary. A
  // missing translation is a cosmetic defect; a white screen is not.
  if (typeof template !== 'string') return String(template ?? '');
  return template.replace(/\{(\w+)\}/g, (_, key) =>
    String(vars?.[key] ?? modifiers[key] ?? `{${key}}`),
  );
}

/** Reactive hook — re-renders component on language change.
 *  `t` is memoized on `[language]` so consumers can safely list it in effect/useCallback
 *  dep arrays without triggering a refire on every render (fixed the /s/:token loop). */
export function useTranslation() {
  const language = useUIStore(s => s.language);
  const dict = translations[language] ?? en;

  const t = useCallback((key: TranslationKey, vars?: Record<string, string | number>): string => {
    const template = dict[key] ?? en[key] ?? key;
    return interpolate(template, vars);
  }, [language]); // dict derives from language; rebuilt together — no separate dep needed

  return { t, language };
}

/** Standalone t() for use outside React (store actions, event handlers, confirm/alert). */
export function t(key: TranslationKey, vars?: Record<string, string | number>): string {
  const language = useUIStore.getState().language;
  const dict = translations[language] ?? en;
  const template = dict[key] ?? en[key] ?? key;
  return interpolate(template, vars);
}

export type { TranslationKey };
