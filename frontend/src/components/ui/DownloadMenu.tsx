/** Unified download menu — one trigger ("Download ▾") opening a dropdown with
 * pdf / docx / markdown export items (driven by useDocumentExport → backend
 * `/api/documents/{id}/export`) plus an optional raw original-file item for
 * references that carry an uploaded file.
 *
 * ARCH: Replaces the divergent document/reference download buttons; both now
 * share one export path (backend already inlines ref images for every format).
 * ARCH: built on the shared Popover primitive (open state + outside-click +
 * positioned panel) — only the trigger + item markup are local.
 * INVARIANT: render nothing when there is neither text to export nor an
 * original file (empty media reference with no transcription).  Why: the menu renders nothing when there's no exportable text and no original file (empty media ref with no transcription) — no empty menu.
 *
 * Theming (`theme` prop, driven by `data-theme` on the root + index.css):
 *   default — standard gray (normal document).
 *   green   — snapshot open: solid dark-green trigger + green panel, mirroring
 *             the green SnapshotPreviewBanner for through-indication.
 *   blue    — reference: blue panel in the reference color. The trigger inherits
 *             its blue tint from the surrounding `.reference-viewer-banner`. */
import { Download, ChevronDown } from 'lucide-react';
import { Button } from './Button';
import { IconButton } from './IconButton';
import { Popover } from './Popover';
import { useDocumentExport, type ExportFormat } from '../../hooks/useDocumentExport';
import { useTranslation } from '../../i18n';

export interface OriginalFile {
  url: string;
  ext: string;
  name: string;
}

export type DownloadMenuTheme = 'default' | 'green' | 'blue';

interface Props {
  documentId: string;
  hasText: boolean;
  original?: OriginalFile;
  theme?: DownloadMenuTheme;
  /** When set, exports THIS checkpoint (snapshot) content instead of the live
   *  document — used while a snapshot preview is open. */
  checkpointId?: string;
  /** Export formats offered, defaults to all. A PDF reference passes
   *  ['docx', 'md']: its `original` item already IS a .pdf, and a second pdf
   *  rebuilt from the recognized text is the wrong file to hand back. */
  exportFormats?: ExportFormat[];
  /** Icon-only trigger (IconButton, title = download) — for icon rows like the
   *  chat header / right-panel plaque, where a labelled button does not fit. */
  iconOnly?: boolean;
}

const ALL_FORMATS: ExportFormat[] = ['pdf', 'docx', 'md'];

export function DownloadMenu({ documentId, hasText, original, theme = 'default', checkpointId, exportFormats = ALL_FORMATS, iconOnly = false }: Props) {
  const { t } = useTranslation();
  const { exportingFormat, exportFormat } = useDocumentExport();

  if (!hasText && !original) return null;

  const busy = exportingFormat !== null;

  // Single-option case (no text to export, just the raw original file): skip
  // the dropdown entirely and download directly on click. Renders the same
  // Button used as the multi-option trigger (not an <a>) so the surrounding
  // theme CSS (.reference-viewer-banner button:hover / .download-menu[data-theme]
  // > button:hover) still applies — an <a> wouldn't match those `button` selectors.
  if (!hasText && original) {
    const downloadOriginal = () => {
      const a = document.createElement('a');
      a.href = original.url;
      a.download = original.name;
      a.click();
    };
    return (
      <div className="download-menu" data-theme={theme}>
        {iconOnly ? (
          <IconButton size="sm" title={t('download')} onClick={downloadOriginal}>
            <Download size={14} />
          </IconButton>
        ) : (
          <Button variant="ghost" size="sm" onClick={downloadOriginal}>
            <Download size={13} />
            {t('download')}
          </Button>
        )}
      </div>
    );
  }

  return (
    <Popover
      rootClassName="download-menu"
      rootDataTheme={theme}
      align="right"
      placement="bottom"
      keyboardNav={false}
      panelClassName="download-menu__panel min-w-[120px]"
      trigger={iconOnly ? (
        <IconButton size="sm" title={t('download')}>
          <Download size={14} />
        </IconButton>
      ) : (
        <Button variant="ghost" size="sm">
          <Download size={13} />
          {t('download')}
          <ChevronDown size={12} />
        </Button>
      )}
    >
      {({ close }) => (
        <>
          {hasText && exportFormats.map(fmt => (
            <div
              key={fmt}
              role="button"
              tabIndex={0}
              className="download-menu__item block w-full text-left px-3 py-2 text-sm cursor-pointer text-text hover:bg-surface2 disabled:opacity-40 disabled:cursor-default"
              onClick={() => {
                if (busy) return;
                exportFormat(documentId, fmt, checkpointId);
              }}
              onKeyDown={e => { if (!busy && (e.key === 'Enter')) exportFormat(documentId, fmt, checkpointId); }}
            >
              {exportingFormat === fmt ? <span className="animate-pulse">{t('infoExporting')}</span> : `*.${fmt}`}
            </div>
          ))}
          {original && (
            <a
              className="download-menu__item block w-full text-left px-3 py-2 text-sm cursor-pointer text-text hover:bg-surface2"
              href={original.url}
              download={original.name}
              onClick={close}
            >
              *{original.ext}
            </a>
          )}
        </>
      )}
    </Popover>
  );
}
