/** Shared document export hook — fetches `/api/documents/{id}/export?format=…`,
 * resolves the filename from Content-Disposition (via the pure helper), and
 * triggers an anchor download. Surface toasts on failure.
 *
 * ARCH: One export path for both documents and references — the backend
 * endpoint already handles references, live-session content, and image inlining. */
import { useState, useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { useTranslation } from '../i18n';
import { parseContentDispositionFilename } from '../utils/content-disposition';

export type ExportFormat = 'pdf' | 'docx' | 'md';

export function useDocumentExport() {
  const [exportingFormat, setExportingFormat] = useState<ExportFormat | null>(null);
  const showToast = useAppStore(s => s.showToast);
  const { t } = useTranslation();

  const exportFormat = useCallback(async (documentId: string, format: ExportFormat, checkpointId?: string) => {
    setExportingFormat(format);
    try {
      const qs = new URLSearchParams({ format });
      if (checkpointId) qs.set("checkpoint_id", checkpointId);
      const resp = await fetch(`/api/documents/${documentId}/export?${qs.toString()}`, { credentials: 'include' });
      if (!resp.ok) throw new Error('Export failed');
      const blob = await resp.blob();
      const cd = resp.headers.get('content-disposition') || '';
      const filename = parseContentDispositionFilename(cd, `document.${format}`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      showToast(t('infoExportFailed'), 'error');
    } finally {
      setExportingFormat(null);
    }
  }, [showToast, t]);

  return { exportingFormat, exportFormat };
}
