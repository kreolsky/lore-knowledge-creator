/** Lazy hydration of a chat message's base64 images.
 *
 * ARCH: list_messages OMITs the heavy `images` column and sends
 * only `image_count`; the actual data URIs come from GET
 * /chat/sessions/{sid}/messages/{mid}/images. This hook resolves the images a bubble
 * should render — the persisted array when already hydrated (create echo / optimistic
 * send), otherwise a mount-time fetch — and exposes ensureImages() for the
 * fork/resend path, which needs the URIs in hand before it dispatches.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '../../api/client';
import type { ChatMessage } from '../../types';

interface LazyImages {
  /** Images to render: persisted when present, else the fetched set (undefined while loading). */
  images: string[] | undefined;
  /** How many images the message has (from image_count, or the persisted array). */
  count: number;
  /** True when the lazy fetch failed — the bubble shows an error placeholder (no silent degradation). */
  error: boolean;
  /** Resolve the URIs (fetching if needed) — used by fork/resend before dispatch. */
  ensureImages: () => Promise<string[]>;
}

export function useLazyMessageImages(message: ChatMessage): LazyImages {
  const persisted = message.images;
  const count = message.image_count ?? persisted?.length ?? 0;
  const [fetched, setFetched] = useState<string[]>();
  const [error, setError] = useState(false);
  const promiseRef = useRef<Promise<string[]> | null>(null);

  const fetchImages = useCallback((): Promise<string[]> => {
    if (!promiseRef.current) {
      promiseRef.current = apiClient
        .get(`/chat/sessions/${message.chat_id}/messages/${message.message_id}/images`)
        .then((r: { images: string[] }) => r.images ?? []);
    }
    return promiseRef.current;
  }, [message.chat_id, message.message_id]);

  // Fetch on mount when the message HAS images but they are not hydrated yet.
  useEffect(() => {
    if (persisted !== undefined || count === 0) return;
    let cancelled = false;
    setError(false);
    fetchImages()
      .then((imgs) => { if (!cancelled) setFetched(imgs); })
      .catch(() => {
        if (!cancelled) {
          setError(true);
          promiseRef.current = null; // allow a later ensureImages/remount to retry
        }
      });
    return () => { cancelled = true; };
  }, [persisted, count, fetchImages]);

  const ensureImages = useCallback(async (): Promise<string[]> => {
    if (persisted !== undefined) return persisted;
    if (fetched !== undefined) return fetched;
    if (count === 0) return [];
    const imgs = await fetchImages();
    setFetched(imgs);
    return imgs;
  }, [persisted, fetched, count, fetchImages]);

  return { images: persisted ?? fetched, count, error, ensureImages };
}
