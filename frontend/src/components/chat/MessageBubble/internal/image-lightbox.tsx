/** Generated-image thumbnail + shared full-image lightbox, split out of
 * MessageBubble.tsx (behavior-preserving). */

import { useEffect, useState } from 'react';
import { AlertTriangle, Download, Trash2 } from 'lucide-react';
import { IconButton, Modal } from '../../../ui';
import { useArmedAction } from '../../../../hooks/useArmedAction';
import { useTranslation } from '../../../../i18n';
import { referenceFileUrl, referenceThumbUrl } from '../../../../utils/reference-url';

/**
 * The generated-image thumbnail inside a generate_image agent-step chip. Click →
 * invokes onOpen (the parent opens the SHARED pin lightbox at this index). A
 * failed thumbnail fetch surfaces an explicit error placeholder (no-silent-
 * degradation — reuses the image-error pattern from the message bubble).
 *
 * The thumbnail IS the clickable surface (an image-wrapping button). The closed-
 * prop Button/IconButton cannot express this layout, matching the ToolPlate
 * disclosure-header precedent.
 *
 * The full-image URL is built by generatedImageUrl (below) — no filePath is
 * needed on the step.
 */
export function GeneratedImageThumb({ imageRefId, onOpen }: { imageRefId: string; onOpen: () => void }) {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);

  if (errored) {
    return (
      <div className="text-xs text-red-500 flex items-center gap-1">
        <AlertTriangle size={12} />
        {t('failedToLoadImages')}
      </div>
    );
  }

  return (
    // eslint-disable-next-line react/forbid-elements
    <button
      type="button"
      onClick={onOpen}
      aria-label={t('viewGeneratedImage')}
      title={t('viewGeneratedImage')}
      className="block border border-border cursor-pointer hover:opacity-80 transition-opacity"
    >
      {!loaded && (
        <div className="w-[180px] h-[135px] bg-surface2 animate-pulse" />
      )}
      <img
        src={referenceThumbUrl(imageRefId)}
        alt={t('generatedImage')}
        onLoad={() => setLoaded(true)}
        onError={() => setErrored(true)}
        className={`max-w-[180px] max-h-[180px] object-contain ${loaded ? 'block' : 'hidden'}`}
      />
    </button>
  );
}

/**
 * The full-image file URL of a generated image. The file route resolves by
 * reference_id and uses the basename ONLY as the Content-Disposition download name
 * (which outranks the link's `download` attribute), so the basename is the name a
 * download gets: `{document title}-{n}.png`, or `image-{n}.png` with no title.
 */
export function generatedImageUrl(imageRefId: string, title: string, n: number): string {
  // Path- and filesystem-hostile characters go; encodeURIComponent keeps
  // spaces and non-ASCII titles intact through the path segment.
  const stem = title.replace(/[\\/?#%:*"<>|]/g, '_').trim() || 'image';
  return referenceFileUrl(imageRefId, encodeURIComponent(`${stem}-${n}.png`));
}

/**
 * The full image + its no-silent-degradation error fallback, owned per displayed
 * image. Keyed by `src` (see ImageLightbox) so navigating to a new image remounts
 * it with a fresh errored=false — a previously broken image never taints the next
 * one (no stale-error flash, no reset effect needed). `src` is a pre-built URL:
 * generated-image callers pass referenceFileUrl(id, 'full.png'); user-attachment
 * callers pass the data URI directly — the two lightboxes are literally identical
 *.
 */
/** Download filename for a lightbox src: a URL already names its file (the
 * basename — generatedImageUrl); a data-URI attachment is numbered by position,
 * its extension taken from the mime. */
function downloadName(src: string, n: number): string {
  if (!src.startsWith('data:')) return decodeURIComponent(src.split('/').pop() ?? '');
  const ext = src.slice('data:image/'.length, src.indexOf(';')).replace('jpeg', 'jpg');
  return `image-${n}.${ext || 'png'}`;
}

function LightboxImage({ src, n, onDelete, deleteArmed, onDeleteDisarm }: {
  src: string; n: number;
  onDelete?: () => void; deleteArmed?: boolean; onDeleteDisarm?: () => void;
}) {
  const { t } = useTranslation();
  const [errored, setErrored] = useState(false);
  const trash = onDelete && (
    <IconButton
      size="md"
      danger
      filled={deleteArmed}
      title={t('deleteImage')}
      aria-label={t('deleteImage')}
      onClick={onDelete}
      onMouseLeave={onDeleteDisarm}
    >
      <Trash2 size={16} />
    </IconButton>
  );
  const plateClass = 'absolute top-2 right-2 z-10 flex items-center gap-1 p-1 bg-surface opacity-0 group-hover:opacity-100 focus-within:opacity-100 [@media(hover:none)]:opacity-100';
  if (errored) {
    return (
      <>
        <div className="text-xs text-red-500 flex items-center gap-1 justify-center min-h-[200px] min-w-[280px] px-4 text-center">
          <AlertTriangle size={12} />
          {t('failedToLoadImages')}
        </div>
        {/* A broken image is still cullable (the reference row exists while
            only its file failed to load): the plate keeps the trash, not the
            download. */}
        {trash && <div className={plateClass}>{trash}</div>}
      </>
    );
  }
  return (
    <>
      <img
        src={src}
        alt={t('generatedImage')}
        onError={() => setErrored(true)}
        className="block max-w-full max-h-[72vh] object-contain border border-border"
      />
      {/* Same hover plate as the reference image overlay (ReferenceMediaBar): top-right
          corner, shown on hover / focus-within, always on touch devices; z-10 lifts
          it above the nav zones. A broken image offers no download (its branch
          above keeps only the trash). The trash (left of download, generated-image callers only)
          is the same armed two-click delete as the thumb. */}
      <div className={plateClass}>
        {trash}
        <a
          href={src}
          download={downloadName(src, n)}
          title={t('download')}
          aria-label={t('download')}
          className="flex items-center justify-center w-[30px] h-[30px] text-text-muted hover:text-text hover:bg-surface3"
        >
          <Download size={16} />
        </a>
      </div>
    </>
  );
}

/**
 * The shared full-image lightbox for one image set
 * (N images). ←/→ keyboard arrows + clicking the left/right third of the image
 * cycle with wrap-around; a "{n} / {total}" counter sits below the image.
 * Navigation + counter are HIDDEN for single-image sets (srcs.length <= 1) — they
 * render a plain lightbox, unchanged from the legacy per-thumb behavior.
 *
 * ARCH: src-based, not reference-ID-based.
 * Generated-image callers build the URL list from reference IDs (referenceFileUrl);
 * user-attachment callers pass the data URIs directly. This is the ONLY component
 * for full-image viewing, so the two windows are guaranteed byte-identical.
 *
 * The nav zones are two invisible, aria-labeled <button> overlays sized to the
 * image's side thirds (no visible arrows) — they give screen-reader nav, a
 * focusable Tab target, and a cursor-pointer affordance for mouse/touch, all
 * without splitting the image. The document-level keydown listener is registered
 * only while this component is mounted (openIndex !== null in the parent), so
 * exactly one listener exists at a time. Escape is left to the Modal overlay
 * (Modal.tsx); the zone buttons ride the Modal's existing Tab focus-trap.
 * border-radius: 0 on every element (project rule); the Modal shell is the only
 * rounded exception.
 *
 * `onDelete(i)` (optional, generated-image callers only — the user-attachment
 * lightbox passes nothing and is unchanged) adds the armed trash to the hover
 * plate and the `Delete` key (press twice; Backspace is the same key on macOS
 * keyboards). One armed state serves both, so a keyed delete cannot fire on a
 * half-armed click.
 */
export function ImageLightbox({
  srcs,
  index,
  onIndexChange,
  onClose,
  title,
  onDelete,
}: {
  srcs: string[];
  index: number;
  onIndexChange: (i: number) => void;
  onClose: () => void;
  title: string;
  onDelete?: (i: number) => void;
}) {
  const { t } = useTranslation();
  const current = srcs[index];
  const multi = srcs.length > 1;
  const deleteAction = useArmedAction();
  // Single source of each wrap-around direction — reused by both the keydown
  // handler and the nav-zone onClicks so keyboard and mouse nav cannot diverge.
  const goNext = () => onIndexChange((index + 1) % srcs.length);
  const goPrev = () => onIndexChange((index - 1 + srcs.length) % srcs.length);
  const requestDelete = () => {
    if (onDelete) deleteAction.handleClick(() => onDelete(index));
  };
  // Destructured: `deleteAction` itself is a fresh object every render, while
  // `disarm` is a stable useCallback — the effect deps want the stable ref.
  const { disarm: disarmDelete } = deleteAction;

  // Navigating away disarms a half-armed delete — the armed state belongs to
  // the image it was pressed on (mouse-leave parity for the keyboard path:
  // arm on A, flip to B, and the next Delete press must ARM, not fire).
  useEffect(() => { disarmDelete(); }, [index, disarmDelete]);

  // Document-scoped arrow navigation (wrap-around). Mounted only while open, so
  // only one such listener ever exists. Re-subscribes on index change so the
  // closure always holds the latest index.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight') {
        e.preventDefault();
        goNext();
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        goPrev();
      } else if (onDelete && (e.key === 'Delete' || e.key === 'Backspace')) {
        e.preventDefault();
        requestDelete();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
    // goNext/goPrev/requestDelete close over index + srcs.length + onDelete;
    // re-subscribe when they change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, srcs.length, onDelete]);

  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      width={720}
      closeLabel={t('close')}
    >
      <div className="flex items-center justify-center">
        {/* relative wrapper so the invisible nav zones overlay the image thirds. */}
        <div className="relative group inline-block max-w-full">
          <LightboxImage
            key={current}
            src={current}
            n={index + 1}
            onDelete={onDelete ? requestDelete : undefined}
            deleteArmed={deleteAction.armed}
            onDeleteDisarm={deleteAction.disarm}
          />
          {multi && (
            <>
              {/* eslint-disable-next-line react/forbid-elements */}
              <button
                type="button"
                aria-label={t('previousImage')}
                title={t('previousImage')}
                onClick={goPrev}
                className="absolute inset-y-0 left-0 w-1/3 cursor-pointer bg-transparent"
              />
              {/* eslint-disable-next-line react/forbid-elements */}
              <button
                type="button"
                aria-label={t('nextImage')}
                title={t('nextImage')}
                onClick={goNext}
                className="absolute inset-y-0 right-0 w-1/3 cursor-pointer bg-transparent"
              />
            </>
          )}
        </div>
      </div>
      {multi && (
        <div className="text-center text-xs text-text-dim mt-2">
          {t('imageCounter', { n: index + 1, total: srcs.length })}
        </div>
      )}
    </Modal>
  );
}
