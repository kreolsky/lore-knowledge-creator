/** The verdict of an MCP connection check, shared by the admin form and the cabinet (SYSTEM: mcp-servers). */

import type { McpCheckResult, McpCheckStatus } from '../api/admin';
import { useTranslation, type TranslationKey } from '../i18n';

const STATUS_LABELS: Record<Exclude<McpCheckStatus, 'ok'>, TranslationKey> = {
  unauthorized: 'mcpCheckUnauthorized',
  refused: 'mcpCheckRefused',
  unreachable: 'mcpCheckUnreachable',
  protocol: 'mcpCheckProtocol',
};

/** `result` null = still checking; `error` = the check itself could not run. */
export function McpCheckLine({ result, error }: { result: McpCheckResult | null; error?: string }) {
  const { t } = useTranslation();
  if (error) return <p className="text-xs text-red">{error}</p>;
  if (!result) return <p className="text-xs text-text-dim">{t('mcpCheckRunning')}</p>;
  if (result.status === 'ok') {
    return <p className="text-xs text-green">{t('mcpCheckOk', { count: result.tools ?? 0 })}</p>;
  }
  return <p className="text-xs text-red">{t(STATUS_LABELS[result.status])}</p>;
}
