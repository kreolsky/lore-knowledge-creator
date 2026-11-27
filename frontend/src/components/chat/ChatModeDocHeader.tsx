/** Chat mode's right document header: pin/unpin the follow, pick a document, open it in docs mode. */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FolderTree, Maximize2, Pin } from 'lucide-react';
import { IconButton } from '../ui';
import { DocumentPickerPopup } from '../DocumentPickerPopup';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { openDocument } from '../../navigation/open-document';
import { unpinChatDoc } from '../../hooks/useChatDocFollow';
import { useTranslation } from '../../i18n';

export function ChatModeDocHeader() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const documents = useAppStore(s => s.documents);
  const currentDocId = useAppStore(s => s.currentDocument?.document_id ?? null);
  const pinned = useUIStore(s => s.chatDocPinned);
  const setChatDocPinned = useUIStore(s => s.setChatDocPinned);
  const setSidebarTab = useUIStore(s => s.setSidebarTab);
  const [pickerAnchor, setPickerAnchor] = useState<DOMRect | null>(null);

  return (
    <div className="flex items-center gap-0.5 px-2 py-1 min-h-[40px] border-b border-border bg-bg shrink-0">
      <IconButton
        pressed={pinned}
        title={pinned ? t('chatDocUnpin') : t('chatDocPin')}
        aria-label={pinned ? t('chatDocUnpin') : t('chatDocPin')}
        onClick={() => (pinned ? unpinChatDoc(navigate) : setChatDocPinned(true))}
      >
        <Pin size={15} />
      </IconButton>
      <IconButton
        title={t('chatDocPick')}
        aria-label={t('chatDocPick')}
        onClick={e => setPickerAnchor(e.currentTarget.getBoundingClientRect())}
      >
        <FolderTree size={15} />
      </IconButton>
      <div className="flex-1" />
      {currentDocId && (
        <IconButton
          title={t('chatDocOpenFully')}
          aria-label={t('chatDocOpenFully')}
          onClick={() => setSidebarTab('docs')}
        >
          <Maximize2 size={15} />
        </IconButton>
      )}
      {pickerAnchor && (
        <DocumentPickerPopup
          documents={documents}
          currentId={currentDocId}
          anchorDocId={currentDocId}
          anchorRect={pickerAnchor}
          onClose={() => setPickerAnchor(null)}
          // The pick changes the document without the follow's mark, so useChatDocFollow pins it.
          onSelect={id => { setPickerAnchor(null); if (id) openDocument(id, { navigate }); }}
        />
      )}
    </div>
  );
}
