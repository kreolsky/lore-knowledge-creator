/** Section heading with full-width underline and optional description. */
// ARCH: Used by right-panel tabs (settings, links, refs) to give every section
// a uniform visual rhythm: uppercase title → full-width rule → optional caption.
// The -mx-1.5 negates the parent panel's px-1.5 so the underline spans the tab.

import type { ReactNode } from 'react';

interface Props {
  icon?: ReactNode;
  title: string;
  description?: string;
  /** First section in a panel — uses tighter top padding. */
  first?: boolean;
}

export function SectionHeader({ icon, title, description, first }: Props) {
  return (
    <div className={`-mx-1.5 px-5 ${first ? 'pt-3' : 'pt-6'} pb-2 mb-2`}>
      <div className="text-xs font-semibold text-text-muted uppercase tracking-wide flex items-center gap-1.5 pb-1.5 border-b border-border">
        {icon}
        {title}
      </div>
      {description && (
        <p className="text-ui-xs text-text-dim mt-1.5 normal-case font-normal tracking-normal">
          {description}
        </p>
      )}
    </div>
  );
}
