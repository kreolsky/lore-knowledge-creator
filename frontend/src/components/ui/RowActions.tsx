/**
 * RowActions — shared hover-reveal row-action cluster for ListPill consumers.
 * // SYSTEM: row-actions — reusable primary + gear-expand menu cluster for list pills
 *
 * Extracted verbatim from RefCard's .actions/.gearZone. `primary` buttons stay inline;
 * `menu` buttons hide behind a gear that expands on 400ms hover or on click/tap. Reveal is driven by
 * the parent ListPill's `group` class (group-hover) or `active`.
 *
 * ARCH: the gear opens on hover over .gearZone (400ms delay) and closes only on
 * mouseleave from the whole .actions container — so moving the cursor between the
 * expanded menu buttons and the inline primary buttons does not collapse it.
 */

import type React from 'react';
import { Menu } from 'lucide-react';
import { useGearMenu } from '../../hooks/useGearMenu';
import styles from './RowActions.module.css';

interface RowActionsProps {
  /** Always-inline buttons (e.g. Bot / Chat / Embed). */
  primary?: React.ReactNode;
  /** Buttons revealed when the gear expands (e.g. Retry / Delete / Rename / ChangeParent). */
  menu?: React.ReactNode;
  /** Marks the cluster as active — stays revealed and, with activeBackdrop, tints the plate. */
  active?: boolean;
  /** Backdrop tint when active: default surface3 gradient, 'blue' for the open reference. */
  activeBackdrop?: 'blue';
}

export function RowActions({ primary, menu, active = false, activeBackdrop }: RowActionsProps) {
  const gear = useGearMenu();

  // `active` keeps the cluster revealed; `activeBackdrop='blue'` additionally tints
  // the plate blue (the functional open-reference indicator).
  const activeCls = active ? styles.active : '';
  const backdropCls = active && activeBackdrop === 'blue' ? styles.activeBlue : '';

  return (
    <div
      ref={gear.ref}
      className={`${styles.actions} ${activeCls} ${backdropCls}`}
      onClick={e => e.stopPropagation()}
      onMouseLeave={gear.close}
    >
      {primary}
      {menu && (
        <div className={`${styles.gearZone} ${gear.open ? styles.gearOpen : ''}`} onMouseEnter={gear.handleEnter}>
          <span className={styles.gearIcon} onClick={gear.handleIconClick}>
            <Menu size={13} />
          </span>
          <div className={styles.gearActions}>
            {menu}
          </div>
        </div>
      )}
    </div>
  );
}
