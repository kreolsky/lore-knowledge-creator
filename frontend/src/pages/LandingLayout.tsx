/** Two-column landing frame shared by `/` (login) and `/register/:token`.
 *
 * Left column: brand — logo, taglines, subtext, mini-footer pinned to bottom.
 * Right column (`children`): the surface panel, content starting at 25vh so
 * both pages align their headings with the logo. Presentational only — no
 * store slices, no events.
 */
import type { ReactNode } from 'react';
import { useTranslation } from '../i18n';

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

        {/* Mini footer — pinned to the bottom (copyright only) */}
        <div className="py-6 px-[72px] flex gap-5 text-xs text-text-dim opacity-55">
          <span>{t('landingCopyright')}</span>
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
