// SYSTEM: theme — light/dark theme toggle synced to DOM class
import { useEffect } from 'react';
import { useUIStore } from '../store/ui-store';

export function useTheme() {
  const theme = useUIStore(s => s.theme);
  const setTheme = useUIStore(s => s.setTheme);

  useEffect(() => {
    if (theme === 'dark') {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [theme]);

  const toggleTheme = () => setTheme(theme === 'light' ? 'dark' : 'light');

  return { theme, toggleTheme };
}
