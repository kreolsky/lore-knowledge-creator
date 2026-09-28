/** Left rail for the project list: theme/language toggles + user controls.
 *
 * The old "back to editor" button is gone — /projects reaches an editor by
 * opening a project; /admin and /cabinet mount their own SectionShell with its
 * own back button (plan "admin-cabinet-section-shell").
 */

import { UserControls } from './UserControls';

export function NavigationTabBar() {
  return (
    <div className="left-tab-bar">
      <div className="flex-1" />
      <UserControls layout="vertical" />
    </div>
  );
}
