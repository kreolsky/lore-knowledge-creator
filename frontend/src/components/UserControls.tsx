/** Theme/language toggle, profile/settings/logout controls. Rendered in header or vertical sidebar. */
// ARCH: Extracted from App.tsx — resolves circular dependency with ProjectPage import.
// ARCH: variant='minimal' renders only theme + language — used by the anonymous
//       public-share surface (/s/:token) where there is no profile/admin/logout.

import { useNavigate, useLocation } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import { Moon, Sun, LogOut, Settings, Lock, User } from 'lucide-react';
import { IconButton, Button } from './ui';
import { useTheme } from '../hooks/useTheme';
import { useLanguage } from '../hooks/useLanguage';
import { useTranslation } from '../i18n';
import { resetEditorHost } from './editor/editor-host';
import { isEditorShell } from '../utils/routing';

export function UserControls({
  layout = 'horizontal',
  variant = 'full',
}: {
  layout?: 'horizontal' | 'vertical';
  variant?: 'full' | 'minimal';
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const { toggleTheme } = useTheme();
  const { language, toggleLanguage } = useLanguage();
  const { t } = useTranslation();
  const { currentUser, setCurrentUser, setPinLocked, setSectionOrigin, currentDocument, currentProject } = useAppStore(useShallow(s => ({ currentUser: s.currentUser, setCurrentUser: s.setCurrentUser, setPinLocked: s.setPinLocked, setSectionOrigin: s.setSectionOrigin, currentDocument: s.currentDocument, currentProject: s.currentProject })));

  // Every navigation INTO a section records where the user came from — the
  // SectionShell back button's target + label. Editor shell → the open document
  // (title falls back to the project name); /projects → the list itself;
  // section → section keeps the stored origin (the origin never points at
  // another section). fromProject marks an editor-shell entry so the section's
  // left rail can show the project's Documents tab as a return icon.
  const openSection = (path: '/cabinet' | '/admin') => {
    if (isEditorShell(location.pathname)) {
      setSectionOrigin({
        path: location.pathname,
        label: currentDocument?.title ?? currentProject?.name ?? t('projects'),
        fromProject: true,
      });
    } else if (location.pathname === '/projects') {
      setSectionOrigin({ path: '/projects', label: t('projects'), fromProject: false });
    }
    navigate(path);
  };

  const handleLogout = async () => {
    try {
      await apiClient.post('/auth/logout', {});
    } catch {
      // Ignore errors — cookie will be invalid anyway
    }
    resetEditorHost({ scope: 'user' });
    // setCurrentUser(null) keeps its internal clearLastSavedBlobs() as an idempotent
    // safety net for direct callers that bypass this logout path.
    setCurrentUser(null);
    navigate('/');
  };

  // Minimal variant (anonymous public share): theme + language only.
  if (variant === 'minimal') {
    if (layout === 'vertical') {
      return (
        <div className="flex flex-col items-center gap-0.5 pb-2 w-full">
          <IconButton onClick={toggleTheme} title={t('toggleTheme')}>
            <Moon size={15} className="icon-moon" />
            <Sun size={15} className="icon-sun" />
          </IconButton>
          <IconButton onClick={toggleLanguage} title={language === 'en' ? 'Русский' : 'English'}>
            <span className="text-ui-2xs font-bold leading-none">{language.toUpperCase()}</span>
          </IconButton>
        </div>
      );
    }
    return (
      <>
        <IconButton onClick={toggleTheme} title={t('toggleTheme')}>
          <Moon size={15} className="icon-moon" />
          <Sun size={15} className="icon-sun" />
        </IconButton>
        <IconButton onClick={toggleLanguage} title={language === 'en' ? 'Русский' : 'English'}>
          <span className="text-ui-2xs font-bold leading-none">{language.toUpperCase()}</span>
        </IconButton>
      </>
    );
  }

  if (layout === 'vertical') {
    return (
      <div className="flex flex-col items-center gap-0.5 pb-2 w-full">
        <IconButton onClick={toggleTheme} title={t('toggleTheme')}>
          <Moon size={15} className="icon-moon" />
          <Sun size={15} className="icon-sun" />
        </IconButton>
        <IconButton onClick={toggleLanguage} title={language === 'en' ? 'Русский' : 'English'}>
          <span className="text-ui-2xs font-bold leading-none">{language.toUpperCase()}</span>
        </IconButton>
        <div className="w-4 h-px bg-border my-1" />
        {/* On /cabinet and /admin these buttons ARE the section's tab (the
            shell has no top tabs there), so they take the left-bar-tab active chrome. */}
        <IconButton
          onClick={() => openSection('/cabinet')}
          title={currentUser?.name ?? t('profileSettings')}
          className={location.pathname === '/cabinet' ? 'left-bar-tab active' : undefined}
        >
          <User size={15} />
        </IconButton>
        {/* Capability flag from /me (admin | moderator) — never a role string
            comparison here: the flag is the single gate for the section. */}
        {currentUser?.can_manage_users && (
          <IconButton
            onClick={() => openSection('/admin')}
            title={t('adminPanel')}
            className={location.pathname === '/admin' ? 'left-bar-tab active' : undefined}
          >
            <Settings size={15} />
          </IconButton>
        )}
        {currentUser?.has_pin && (
          <IconButton
            onClick={() => setPinLocked(true)}
            title={t('lockScreen')}
          >
            <Lock size={15} />
          </IconButton>
        )}
        <IconButton
          onClick={handleLogout}
          title={t('signOut')}
        >
          <LogOut size={15} />
        </IconButton>
      </div>
    );
  }

  return (
    <>
      <IconButton onClick={toggleTheme} title={t('toggleTheme')}>
        <Moon size={15} className="icon-moon" />
        <Sun size={15} className="icon-sun" />
      </IconButton>
      <IconButton onClick={toggleLanguage} title={language === 'en' ? 'Русский' : 'English'}>
        <span className="text-ui-2xs font-bold leading-none">{language.toUpperCase()}</span>
      </IconButton>
      <div className="w-px h-5 bg-border mx-1" />
      <Button
        variant="ghost"
        size="sm"
        onClick={() => openSection('/cabinet')}
        title={t('profileSettings')}
      >
        {currentUser?.name}
      </Button>
      {currentUser?.can_manage_users && (
        <IconButton
          onClick={() => openSection('/admin')}
          title={t('adminPanel')}
        >
          <Settings size={15} />
        </IconButton>
      )}
      {currentUser?.has_pin && (
        <IconButton
          onClick={() => setPinLocked(true)}
          title={t('lockScreen')}
        >
          <Lock size={15} />
        </IconButton>
      )}
      <IconButton
        onClick={handleLogout}
        title={t('signOut')}
      >
        <LogOut size={15} />
      </IconButton>
    </>
  );
}
