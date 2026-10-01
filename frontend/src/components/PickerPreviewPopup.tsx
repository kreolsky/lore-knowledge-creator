/** LinkPreviewPopup positioned beside a picker/list box.
 *
 *  The picker-side twin of HoverPreviewPopup: a picker holds a box (its list) and
 *  renders the preview flush beside it. Position is pre-computed via
 *  computeBesideBoxPreviewPosition; the picker passes only the title, the body + fetch state.
 *
 *  Completes the preview-window unification — HoverPreviewPopup covered the hover
 *  sites; this covers the three picker sites (LinkSuggestions, ParentPicker,
 *  ContentPicker) that each previously hand-wired LinkPreviewPopup + a copied
 *  side-decide/clamp block. See SYSTEM: preview-geometry / popup-position. */
// SYSTEM: link-preview-popup — picker-positioned preview adapter

import { LinkPreviewPopup } from './LinkPreviewPopup';

interface PickerPreviewPopupProps {
  pos: { top: number; left: number; maxHeight: number } | null;
  title?: string;
  content?: string;
  imageUrl?: string;
  error?: boolean;
  loading?: boolean;
  popupRef?: (el: HTMLDivElement | null) => void;
}

export function PickerPreviewPopup({ pos, title, content, imageUrl, error, loading, popupRef }: PickerPreviewPopupProps) {
  if (!pos) return null;

  return (
    <LinkPreviewPopup
      visible
      top={pos.top}
      left={pos.left}
      maxHeight={pos.maxHeight}
      title={title}
      content={content}
      imageUrl={imageUrl}
      error={error}
      loading={loading}
      popupRef={popupRef}
    />
  );
}
