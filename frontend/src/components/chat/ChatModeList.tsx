/** Chat mode's left panel: the project's chat list, newest first. Reuses ChatEmptyList (rows exactly as in the chat tab). */

import { ChatEmptyList } from './ChatEmptyList';

export function ChatModeList() {
  return (
    <div data-testid="chat-mode-list" className="flex-1 flex flex-col min-h-0">
      <ChatEmptyList activeColor="tree" />
    </div>
  );
}
