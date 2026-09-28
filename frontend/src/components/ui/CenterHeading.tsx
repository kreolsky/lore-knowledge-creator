/** Admin-center section heading: ui-md semibold title over a full-width rule.
 * One style for every sub-section of the admin center — the class literal
 * lives HERE and nowhere else. SectionHeader is the right-panel uppercase
 * style — a different thing. `className` REPLACES the default: a variant
 * (AddUserForm's text-only form inside its flex row, Skills' mb-1 so the
 * hint hugs the rule) passes its full class verbatim. `as` picks the level:
 * h2 where the heading tops a page section whose items carry their own h3.
 */
import type { ReactNode } from 'react';

export function CenterHeading({
  children,
  className = 'text-ui-md font-semibold text-text pb-1.5 border-b border-border mb-3',
  as: Tag = 'h3',
}: {
  children: ReactNode;
  className?: string;
  as?: 'h2' | 'h3';
}) {
  return <Tag className={className}>{children}</Tag>;
}
