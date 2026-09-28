/**
 * Reference-viewer chrome for the editor's reference branch — the back banner
 * (with source-document label) and the non-image media bar. The Editor keeps
 * the render gate; this component owns the back-navigation behavior.
 */
import { useAppStore } from '../../store/app-store';
import { emit } from '../../events';
import type { Reference } from '../../types';
import { ReferenceViewerBanner } from './ReferenceViewerBanner';
import { ReferenceMediaBar } from './ReferenceMediaBar';

interface EditorReferenceBarProps {
  reference: Reference;
  isReadonly: boolean;
  isEmpty: boolean;
}

export function EditorReferenceBar({ reference, isReadonly, isEmpty }: EditorReferenceBarProps) {
  const documents = useAppStore(s => s.documents);
  const referenceSourceDocId = useAppStore(s => s.referenceSourceDocId);
  const setCurrentReference = useAppStore(s => s.setCurrentReference);

  return (
    <div>
      <ReferenceViewerBanner
        reference={reference}
        isReadonly={isReadonly}
        isEmpty={isEmpty}
        backLabel={referenceSourceDocId ? documents.find(d => d.document_id === referenceSourceDocId)?.title : undefined}
        onBack={() => {
          const sourceId = useAppStore.getState().referenceSourceDocId;
          if (sourceId) {
            useAppStore.setState({ referenceSourceDocId: null });
            emit('navigate-to-document', { documentId: sourceId });
          } else {
            setCurrentReference(null);
          }
        }}
      />
      {reference.media_type !== 'image' && (
        <ReferenceMediaBar
          reference={reference}
          canEdit={!isReadonly}
        />
      )}
    </div>
  );
}
