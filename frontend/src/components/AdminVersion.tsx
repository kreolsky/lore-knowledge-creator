/** ` (v0.21.0)` after the admin panel label in the header — mounted only on
 * /admin, so the /api/health read happens there and nowhere else. Loading
 * renders nothing; a failed read says so in red (no silent degradation). */

import { useTranslation } from '../i18n';
import { useAppVersion } from '../hooks/useAppVersion';

export function AdminVersion() {
  const { t } = useTranslation();
  const version = useAppVersion();
  if (version === null) return null;
  if (version === '') return <span className="text-red"> ({t('appVersionUnavailable')})</span>;
  return <span data-testid="admin-version"> ({version})</span>;
}
