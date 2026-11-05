/**
 * Full-image preview for editor images: listens for `editor-image-preview`
 * (ImageWidget click) and shows the chat's ImageLightbox over the HOST
 * document's gallery. A click inside a transclusion or a table cell opens the
 * outermost editor's gallery; an image that gallery does not hold (a table cell's
 * own) opens alone.
 */

import { useEffect, useState } from 'react';
import { EditorView } from '@codemirror/view';
import { on, off } from '../../events';
import type { EventMap } from '../../events';
import { useTranslation } from '../../i18n';
import { ImageLightbox } from '../chat/MessageBubble/internal/image-lightbox';
import { collectGallery, galleryIndexOf } from './image-gallery';

interface Gallery {
  srcs: string[];
  index: number;
}

/** The outermost editor holding `view`, plus the element of `view` directly inside it. */
function hostOf(view: EditorView): { host: EditorView; inner: Element } {
  let host = view;
  let inner: Element = view.dom;
  for (;;) {
    const outerDom = host.dom.parentElement?.closest('.cm-editor');
    const outer = outerDom ? EditorView.findFromDOM(outerDom as HTMLElement) : null;
    if (!outer) return { host, inner };
    inner = host.dom;
    host = outer;
  }
}

function buildGallery({ src, element, editorView }: EventMap['editor-image-preview']): Gallery {
  const { host, inner } = hostOf(editorView);
  const anchor = host === editorView ? element : inner;
  let pos: number | null = null;
  try { pos = host.posAtDOM(anchor); } catch { /* detached: fall back to the first match */ }
  const items = collectGallery(host.state);
  const index = galleryIndexOf(items, src, pos);
  return index < 0 ? { srcs: [src], index: 0 } : { srcs: items.map((i) => i.src), index };
}

export function ImagePreviewHost() {
  const { t } = useTranslation();
  const [gallery, setGallery] = useState<Gallery | null>(null);

  useEffect(() => {
    const open = (payload: EventMap['editor-image-preview']) => setGallery(buildGallery(payload));
    on('editor-image-preview', open);
    return () => off('editor-image-preview', open);
  }, []);

  if (!gallery) return null;
  return (
    <ImageLightbox
      srcs={gallery.srcs}
      index={gallery.index}
      onIndexChange={(index) => setGallery({ ...gallery, index })}
      onClose={() => setGallery(null)}
      title={t('imagePreview')}
    />
  );
}
