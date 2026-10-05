/** Admin info tab — instance storage: DB size on disk + soft-deleted data.

 * Read from GET /api/admin/info/storage, which memoizes the last measurement
 * for 30 min (never polled): opening the tab reads the memo, the Refresh
 * button forces a new measurement, and the time the numbers were taken is
 * always shown so a memoized answer never reads as current. The disk number
 * is the live lore.db directory (volume total shown beside it); the per-table
 * bytes are a LOGICAL estimate — labelled as such, never presented as disk
 * usage. A missing mount renders the backend's disk_error text as an error,
 * distinct from a real number (no silent degradation).
 */

import { useCallback, useEffect, useState } from 'react';
import { Button, CenterHeading } from '../ui';
import { getAdminStorageInfo, type AdminStorageInfo } from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';

function fmtBytes(n: number): string {
  if (!Number.isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

function StatBlock({ label, value, sub, error }: { label: string; value?: string; sub?: string; error?: string }) {
  return (
    <div className="bg-surface2 p-4 flex-1 min-w-0">
      <div className="text-xs text-text-dim mb-1">{label}</div>
      {error !== undefined ? (
        <div className="text-red text-sm" data-info-error>{error}</div>
      ) : (
        <>
          <div className="text-hero tabular-nums" data-info-stat>{value}</div>
          {sub && <div className="text-xs text-text-dim mt-1">{sub}</div>}
        </>
      )}
    </div>
  );
}

export function InfoTab() {
  const [info, setInfo] = useState<AdminStorageInfo | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  // WHY: one run is seconds of serial table scans on the backend's shared DB
  // connection — a second click while one is in flight must not stack another.
  const [loading, setLoading] = useState(false);
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);

  const load = useCallback(async (fresh: boolean) => {
    setLoading(true);
    try {
      const data = await getAdminStorageInfo(fresh);
      setInfo(data);
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      showToast(t('failedToLoadStorageStats'), 'error');
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    load(false);
  }, [load]);

  const tables = info
    ? [...info.tables].sort((a, b) => b.deleted_bytes - a.deleted_bytes)
    : [];

  return (
    <div className="space-y-6">
      <CenterHeading>{t('adminInfo')}</CenterHeading>

      {loadFailed && <p className="text-xs text-red mb-2">{t('failedToLoadStorageStats')}</p>}
      {loading && !info && <p className="text-xs text-text-dim">{t('infoMeasuring')}</p>}

      {info && (
        <>
          <div className="flex gap-4">
            <StatBlock
              label={t('infoDbOnDisk')}
              value={info.disk ? fmtBytes(info.disk.db_bytes) : undefined}
              sub={info.disk ? `${t('infoVolumeTotal')}: ${fmtBytes(info.disk.volume_bytes)}` : undefined}
              error={info.disk_error ?? undefined}
            />
            <StatBlock
              label={t('infoDeletedData')}
              value={fmtBytes(info.totals.deleted_bytes + info.totals.in_deleted_projects_bytes)}
              sub={`${t('infoDeletedRows')}: ${(info.totals.deleted_rows + info.totals.in_deleted_projects_rows).toLocaleString()}`}
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-text-dim">{t('infoLogicalEstimate')}</span>
              <Button size="sm" onClick={() => load(true)} disabled={loading}>
                {loading ? t('infoMeasuring') : t('infoRefresh')}
              </Button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-text-dim border-b border-border">
                    <th className="py-2 pr-3 font-normal">{t('infoTable')}</th>
                    <th className="py-2 pr-3 font-normal text-right">{t('infoLiveRows')}</th>
                    <th className="py-2 pr-3 font-normal text-right">{t('infoLiveBytes')}</th>
                    <th className="py-2 pr-3 font-normal text-right">{t('infoDeletedRows')}</th>
                    <th className="py-2 pr-3 font-normal text-right">{t('infoDeletedBytes')}</th>
                    <th className="py-2 pr-3 font-normal text-right">{t('infoInDeletedProjects')}</th>
                  </tr>
                </thead>
                <tbody>
                  {tables.map(row => (
                    <tr key={row.name} className="border-b border-border-soft">
                      <td className="py-1.5 pr-3">{row.name}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{row.live_rows.toLocaleString()}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{fmtBytes(row.live_bytes)}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{row.deleted_rows.toLocaleString()}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{fmtBytes(row.deleted_bytes)}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {row.in_deleted_projects_rows > 0
                          ? `${row.in_deleted_projects_rows.toLocaleString()} · ${fmtBytes(row.in_deleted_projects_bytes)}`
                          : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="text-xs text-text-dim mt-2">{t('infoMeasured', {
              at: new Date(info.measured_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
              ms: info.measured_ms,
            })}</div>
          </div>
        </>
      )}
    </div>
  );
}
