/** Syncs document.documentElement.lang attribute with store language preference. */

import { useEffect } from 'react';
import { useUIStore } from '../store/ui-store';

export function useLanguage() {
  const language = useUIStore(s => s.language);
  const setLanguage = useUIStore(s => s.setLanguage);

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  const toggleLanguage = () => setLanguage(language === 'en' ? 'ru' : 'en');

  return { language, toggleLanguage };
}
