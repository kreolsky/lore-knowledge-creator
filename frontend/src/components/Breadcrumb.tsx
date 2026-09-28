/** Breadcrumb bar: Project : Ancestor : Title with inline rename (double-click). Events consumed: breadcrumb-start-rename. */

import { Fragment, useState, useRef, useEffect, useCallback } from 'react';
import { AccessLevel } from '../types';
import { useEvent } from '../hooks/useEvent';
import { useEditorView } from '../editor/active-editor';
import { useTranslation } from '../i18n';
import b from './Breadcrumb.module.css';

interface BreadcrumbProps {
  projectName: string;
  ancestors: { id: string; title: string }[];
  documentTitle: string | null;
  isReference: boolean;
  accessLevel: AccessLevel;
  onProjectClick: () => void;
  onAncestorClick: (docId: string) => void;
  onDocumentRename: (newTitle: string) => void;
  // Clicking the active (last) crumb reveals the open document in the tree.
  // Optional: when omitted (public share — no sidebar tree) the crumb is non-interactive.
  onDocumentReveal?: () => void;
}

export function Breadcrumb({ projectName, ancestors, documentTitle, isReference, accessLevel, onProjectClick, onAncestorClick, onDocumentRename, onDocumentReveal }: BreadcrumbProps) {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isRenaming && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isRenaming]);

  const startRename = useCallback(() => {
    if (!documentTitle || accessLevel !== 'full') return;
    setRenameValue(documentTitle);
    setIsRenaming(true);
  }, [documentTitle, accessLevel]);

  useEvent('breadcrumb-start-rename', startRename);

  const commitRename = (focusEditor = false) => {
    const trimmed = renameValue.trim();
    if (trimmed && trimmed !== documentTitle) {
      onDocumentRename(trimmed);
    }
    setIsRenaming(false);
    if (focusEditor) {
      requestAnimationFrame(() => getEditorView()?.focus());
    }
  };

  return (
    <nav className={b.breadcrumb} aria-label="Breadcrumb">
      <span
        className={b.crumbProjectWrap}
        title={accessLevel !== 'full' ? (accessLevel === 'readonly' ? t('readOnlyTitle') : t('commentatorTitle')) : t('showDocuments')}
        onClick={onProjectClick}
      >
        <span className={b.crumbProjectName}>
          {projectName}
          {accessLevel !== 'full' && (
            <span className="bg-[rgba(220,60,60,0.18)] text-red pl-0.5 select-none">
              {accessLevel === 'readonly' ? ` ${t('roAccess')}` : ` ${t('notesAccess')}`}
            </span>
          )}
        </span>
      </span>

      {ancestors.length >= 2 ? (
        <>
          <span className={b.sep}>:</span>
          <span className={b.crumbAncestor} title={ancestors[ancestors.length - 1].title} onClick={() => onAncestorClick(ancestors[ancestors.length - 1].id)}>
            {ancestors[ancestors.length - 1].title}
          </span>
        </>
      ) : (
        ancestors.map(a => (
          <Fragment key={a.id}>
            <span className={b.sep}>:</span>
            <span className={b.crumbAncestor} title={a.title} onClick={() => onAncestorClick(a.id)}>{a.title}</span>
          </Fragment>
        ))
      )}

      {documentTitle && (
        <>
          <span className={b.sep}>:</span>

          <span
            className={b.crumbTitleWrap}
            onClick={!isRenaming && onDocumentReveal ? onDocumentReveal : undefined}
            onDoubleClick={!isRenaming && accessLevel === 'full' ? startRename : undefined}
            title={!isRenaming && onDocumentReveal ? t('revealInTree') : undefined}
          >
            {isRenaming ? (
              <input
                ref={inputRef}
                className={b.crumbRenameInput}
                data-rename-input
                value={renameValue}
                onChange={e => setRenameValue(e.target.value)}
                onBlur={() => commitRename()}
                onKeyDown={e => {
                  if (e.key === 'Enter') commitRename(true);
                  if (e.key === 'Escape') setIsRenaming(false);
                }}
                style={{ width: `${renameValue.length + 1}ch` }}
              />
            ) : (
              <span className={b.crumbActive}>
                {documentTitle}
                {isReference && (
                  <span className="bg-[rgba(60,130,246,0.18)] text-accent pl-0.5 select-none"> {t('refBadge')}</span>
                )}
              </span>
            )}
          </span>
        </>
      )}
    </nav>
  );
}
