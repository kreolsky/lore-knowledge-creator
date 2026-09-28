/**
 * LinkPreviewPopup driven by a useHoverPreview return object.
 *
 * Wires positioning, the popup root ref, and both leave handlers from the hook so call
 * sites pass only what is genuinely theirs: the body (`content`/`imageUrl`) and its
 * fetch state (`error`/`loading`).
 */
// ARCH: completes the useHoverPreview extraction — that one stopped at the hook and left
// the render half (8 lines of mechanical prop projection) duplicated across five sites.
// See SYSTEM: hover-preview. LinkPreviewPopup stays the low-level presentational popup for
// the self-positioning consumers (pickers, EditorLinkPreview) which hold no hover object.

import { useHoverPreview } from '../hooks/useHoverPreview';
import { LinkPreviewPopup } from './LinkPreviewPopup';

interface HoverPreviewPopupProps {
  hover: ReturnType<typeof useHoverPreview>;
  content?: string;
  imageUrl?: string;
  error?: boolean;
  loading?: boolean;
  /** Overrides the hook's own visibility. Only for a site with a product rule of its own. */
  visible?: boolean;
}

export function HoverPreviewPopup({ hover, content, imageUrl, error, loading, visible }: HoverPreviewPopupProps) {
  const { state } = hover;
  if (!state) return null;

  return (
    <LinkPreviewPopup
      visible={visible ?? hover.visible}
      top={state.top}
      bottom={state.bottom}
      left={state.left}
      maxHeight={state.maxHeight}
      content={content}
      imageUrl={imageUrl}
      error={error}
      loading={loading}
      popupRef={hover.popupRootCallback}
      onMouseLeave={hover.handleHoverLeave}
      onDismiss={hover.handleHoverLeave}
    />
  );
}
