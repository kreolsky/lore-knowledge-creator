/** Two-column landing frame shared by `/` (login) and `/register/:token`.
 *
 * Left column: brand — logo, taglines, subtext, mini-footer (repository link) pinned to bottom.
 * Right column (`children`): the surface panel, content starting at 25vh so
 * both pages align their headings with the logo. Presentational only — no
 * store slices, no events.
 */
import type { ReactNode } from 'react';
import { useTranslation } from '../i18n';

const REPOSITORY_URL = 'https://github.com/kreolsky/lore-knowledge-creator';

export function LandingLayout({ children }: { children: ReactNode }) {
  const { t } = useTranslation();

  return (
    <div className="flex h-dvh bg-bg text-text font-sans overflow-hidden">

      {/* ── Left: brand / hero ─────────────────────────────────── */}
      <div className="flex-1 flex flex-col relative overflow-hidden">
        {/* Faint document-tree decoration */}
        <div
          aria-hidden="true"
          className="absolute pointer-events-none select-none whitespace-pre font-mono text-text opacity-[0.04] bottom-[10vh] right-[4vw] text-ui-base leading-[1.8]"
        >
          {'  ── Introduction\n  │   ── Overview\n  │   ── Goals\n  ── Architecture\n  │   ── Decisions\n  │   ── Constraints\n  ── Changelog\n  │   ── v1.0\n  └── References'}
        </div>

        {/* Main content — starts at 25vh */}
        <div className="pt-[25vh] px-[72px]">
          {/* Logo + name */}
          <div className="flex items-center gap-3 mb-10">
            <div className="w-8 h-8 bg-accent text-white flex items-center justify-center text-base font-bold shrink-0">
              L
            </div>
            <span className="text-xl font-bold text-text tracking-[-0.5px]">
              Lore
            </span>
          </div>

          {/* Tagline */}
          <h1
            className="font-[800] leading-[1.1] mb-5 text-text max-w-[380px] text-hero tracking-[-1.5px]"
          >
            {t('landingTagline1')}<br />{t('landingTagline2')}
          </h1>

          {/* Subtext */}
          <p className="text-ui-md text-text-dim max-w-[360px] leading-[1.7]">
            {t('landingSubtext')}
          </p>
        </div>

        {/* Spacer pushes mini-footer to bottom */}
        <div className="flex-1" />

        {/* Mini footer — pinned to the bottom: the public repository link */}
        <div className="py-6 px-[72px] flex">
          <a
            href={REPOSITORY_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 text-ui-md text-text hover:text-accent"
          >
            <svg viewBox="0 0 16 16" width="18" height="18" fill="currentColor" aria-hidden="true">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
            </svg>
            {t('landingGithub')}
          </a>
        </div>
      </div>

      {/* Divider */}
      <div className="w-px bg-border-soft shrink-0" />

      {/* ── Right: page content (login / register form) ────────── */}
      <div
        className="w-[380px] shrink-0 flex flex-col bg-surface px-12 pt-[25vh]"
      >
        {children}
      </div>

    </div>
  );
}
