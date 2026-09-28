/** Banner shown when previewing a document from chat sources. Back button returns to the original document. Store slices: previewDocument, clearPreviewDocument, currentDocument. */

import { ArrowLeft, FileText } from 'lucide-react';
import { useAppStore } from '../../store/app-store';
import { Button } from '../ui';
import { useTranslation } from '../../i18n';

export function PreviewDocumentBanner() {
  const previewDocument = useAppStore(s => s.previewDocument);
  const clearPreviewDocument = useAppStore(s => s.clearPreviewDocument);
  const currentDocument = useAppStore(s => s.currentDocument);
  const { t } = useTranslation();

  if (!previewDocument) return null;

  const backLabel = currentDocument?.title ?? t('back');

  return (
    <div className="snapshot-preview-banner">
      <div className="flex items-center gap-1.5 text-text-dim text-sm min-w-0">
        <FileText size={13} className="shrink-0" />
        <span className="truncate">{previewDocument.title}</span>
      </div>
      <div className="flex gap-1.5 items-center ml-auto">
        <Button variant="ghost" size="sm" onClick={clearPreviewDocument}>
          <ArrowLeft size={13} /> {backLabel}
        </Button>
      </div>
    </div>
  );
}
