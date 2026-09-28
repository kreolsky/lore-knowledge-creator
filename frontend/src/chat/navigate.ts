/** Chat-origin document navigation: open a document while keeping the active chat.
 *
 * Used by the chat-origin navigators — Sources document links (citations above an
 * AI message) and the ChatHeader "go to document" button.
 *
 * ARCH: the active chat is PROJECT-SCOPED and
 * persists across document navigation by default, so this no longer needs to pin
 * the chat to the destination document's per-doc memory. It just emits the
 * navigate event; the chat stays open. The session's ownership (document_id) and
 * stored context are NOT mutated.
 *
 * Kept as a thin named entry point (instead of an inline emit) so the two
 * chat-origin navigators read as "navigate, keep chat" at the call site. */

import { emit } from '../events';
import { useUIStore } from '../store/ui-store';

export function navigateToDocKeepingChat(documentId: string): void {
  useUIStore.getState().pinRightPanel(documentId, 'chat');
  emit('navigate-to-document', { documentId });
}
