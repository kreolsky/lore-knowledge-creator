/** Generated-image thumbnail + shared full-image lightbox, split out of
 * MessageBubble.tsx (behavior-preserving). */

import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Modal } from '../../../ui';
import { useTranslation } from '../../../../i18n';
import { referenceThumbUrl } from '../../../../utils/reference-url';

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
 * The full-image URL uses the file route with a placeholder basename: that route
 * serves by reference_id and ignores the filename for resolution (it only sets
 * the Content-Disposition download name), so no filePath is needed on the step.
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
 * The full image + its no-silent-degradation error fallback, owned per displayed
 * image. Keyed by `src` (see ImageLightbox) so navigating to a new image remounts
 * it with a fresh errored=false — a previously broken image never taints the next
 * one (no stale-error flash, no reset effect needed). `src` is a pre-built URL:
 * generated-image callers pass referenceFileUrl(id, 'full.png'); user-attachment
 * callers pass the data URI directly — the two lightboxes are literally identical
 *.
 */
function LightboxImage({ src }: { src: string }) {
  const { t } = useTranslation();
  const [errored, setErrored] = useState(false);
  if (errored) {
    return (
      <div className="text-xs text-red-500 flex items-center gap-1 justify-center min-h-[200px] min-w-[280px] px-4 text-center">
        <AlertTriangle size={12} />
        {t('failedToLoadImages')}
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={t('generatedImage')}
      onError={() => setErrored(true)}
      className="max-w-full max-h-[72vh] object-contain border border-border"
    />
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
 */
export function ImageLightbox({
  srcs,
  index,
  onIndexChange,
  onClose,
  title,
}: {
  srcs: string[];
  index: number;
  onIndexChange: (i: number) => void;
  onClose: () => void;
  title: string;
}) {
  const { t } = useTranslation();
  const current = srcs[index];
  const multi = srcs.length > 1;
  // Single source of each wrap-around direction — reused by both the keydown
  // handler and the nav-zone onClicks so keyboard and mouse nav cannot diverge.
  const goNext = () => onIndexChange((index + 1) % srcs.length);
  const goPrev = () => onIndexChange((index - 1 + srcs.length) % srcs.length);

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
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
    // goNext/goPrev close over index + srcs.length; re-subscribe when they change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, srcs.length]);

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
        <div className="relative inline-block max-w-full">
          <LightboxImage key={current} src={current} />
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
