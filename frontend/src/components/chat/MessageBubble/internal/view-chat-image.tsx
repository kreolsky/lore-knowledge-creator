/**
 * The `view_chat_image` tool plate — the person's thumbnail of the image the
 * agent saw in another chat, shaped like the generated-image plate: the source
 * chat's title in the header, the bare thumbnail in the body.
 *
 * // SYSTEM: dsh-conversation. The plate's text block IS the contract: the
 * //   plugin's render settles `JSON.stringify(meta)` with
 * //   `{chat_id, message_id, index, chat_title, media_type, width, height}`
 *   (harness-driver/plugin/src/tools.ts renderViewChatImage) for a served
 *   image, the backend's own `unavailable`/error envelopes as plain text
 *   otherwise. This component parses that ONE contract — with an admitted
 *   identity it lazy-fetches the image through the EXISTING owner-gated
 *   `GET /chat/sessions/{chat_id}/messages/{message_id}/images` (the same
 *   route useLazyMessageImages hits) and renders `images[index]` above the
 *   plate. A fetch failure or a 404 (chat A or its message deleted since)
 *   draws the explicit "no longer available" line — a Lore delete retracts
 *   the image from the person's view, never a blank (no-silent-degradation).
 *   `unavailable` envelopes and error strings render as their text only.
 */

import { useEffect, useMemo, useState } from 'react';
import { ImageLightbox, ImageThumb } from './image-lightbox';
import { AgentStepIcon } from './agent-steps';
import { ToolPlate } from '../../ToolPlate';
import { useTranslation } from '../../../../i18n';
import { apiClient } from '../../../../api/client';

/** The thumbnail's fetch identity, read off the settled meta JSON. An
 * `unavailable` envelope carries the same three keys — the `unavailable`
 * member is what tells a link (never fetched, never rendered) from a stored
 * image. */
interface ViewChatImageRef {
  chat_id: string;
  message_id: string;
  index: number;
  chat_title: string;
}

/** Parse the plate's text block as a fetch identity; null for every other
 * shape (errors, `unavailable`, non-vision refusals — anything the plugin
 * rendered as plain text). */
function parseViewChatImageRef(text: string): ViewChatImageRef | null {
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch { return null; }
  if (typeof parsed !== 'object' || parsed === null) return null;
  const o = parsed as Record<string, unknown>;
  if (o.unavailable !== undefined) return null;
  if (typeof o.chat_id !== 'string' || typeof o.message_id !== 'string') return null;
  if (typeof o.index !== 'number' || !Number.isInteger(o.index) || o.index < 0) return null;
  const chat_title = typeof o.chat_title === 'string' ? o.chat_title : '';
  return { chat_id: o.chat_id, message_id: o.message_id, index: o.index, chat_title };
}

/** The settled plate: a served image renders as the thumbnail alone (the meta
 * JSON is the model's record, not the person's); every other result —
 * `unavailable`, errors, non-vision refusals — renders its text, never an
 * empty plate. Thumbnail and lightbox are the generated-image plate's own
 * (ImageThumb + ImageLightbox over the data URI), the lightbox OUTSIDE the
 * plate — a click inside the plate body collapses the plate. */
export function ViewChatImagePlate({ text, isError, defaultExpanded }: {
  text: string; isError: boolean; defaultExpanded: boolean;
}) {
  const { t } = useTranslation();
  const ref = useMemo(() => parseViewChatImageRef(text), [text]);
  const [state, setState] = useState<{ uri: string } | 'loading' | 'gone'>('loading');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!ref) return;
    let cancelled = false;
    setState('loading');
    apiClient
      .get(`/chat/sessions/${ref.chat_id}/messages/${ref.message_id}/images`)
      .then((r: { images?: string[] }) => {
        if (cancelled) return;
        // Index out of range reads as gone: the message's images are written
        // only at create, so a shorter array means the row is not the one the
        // agent read — never a blank thumb.
        const uri = (r.images ?? [])[ref.index];
        setState(typeof uri === 'string' && uri !== '' ? { uri } : 'gone');
      })
      .catch(() => { if (!cancelled) setState('gone'); });
    return () => { cancelled = true; };
  }, [ref]);

  const label = t('chatImage');
  return (
    <>
      <ToolPlate
        icon={<AgentStepIcon outcome={isError ? 'failed' : undefined} />}
        defaultExpanded={defaultExpanded}
        // The generated-image plate's header shape: the label is a protected
        // zone, the chat title ellipsizes instead of wrapping the header.
        title={ref?.chat_title
          ? <span className="flex items-center gap-1 min-w-0 w-full">
              <span className="shrink-0">{label}</span>
              <span className="opacity-50 shrink-0">·</span>
              <span className="truncate min-w-0 flex-1 opacity-70">{ref.chat_title}</span>
            </span>
          : <span>{label}</span>}
        tone={isError ? 'failed' : undefined}
        ariaLabel={isError ? `${t('agentStepFailed')}: ${label}` : undefined}
      >
        {!ref
          ? <pre className="text-xs font-mono whitespace-pre-wrap break-words m-0">{text}</pre>
          : state === 'loading'
            ? <div className="w-[180px] h-[135px] bg-surface2 animate-pulse" />
            : state === 'gone'
              ? <div className="text-ui-sm text-text-dim">{t('viewChatImageGone')}</div>
              : (
                <ImageThumb
                  src={state.uri}
                  label={t('viewAttachmentImage')}
                  alt={t('attachmentImage')}
                  onOpen={() => setOpen(true)}
                />
              )}
      </ToolPlate>
      {open && typeof state === 'object' && (
        <ImageLightbox
          srcs={[state.uri]}
          index={0}
          onIndexChange={() => {}}
          onClose={() => setOpen(false)}
          title={t('attachmentImage')}
        />
      )}
    </>
  );
}
