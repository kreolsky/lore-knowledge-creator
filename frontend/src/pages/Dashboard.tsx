/** Project list split into own / shared sections, create-project form, and inline card rename. Rendered at /projects route. */
// ARCH: Extracted from App.tsx — page component with its own data fetching.

import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import type { AccessLevel, Project } from '../types';
import { docUrl } from '../utils/routing';
import { FolderPlus, Pencil, Users } from 'lucide-react';
import { Button, FieldInput, IconButton } from '../components/ui';
// WHY: direct path, not the '../components/ui' barrel — the Dashboard tests
// mock the barrel with only the interactive components.
import { CenterHeading } from '../components/ui/CenterHeading';
import { useTranslation } from '../i18n';

/** Access badge on a shared project card: full = green, commentator =
 * dark sticky yellow, readonly = red. */
const ACCESS_BADGE: Record<AccessLevel, { label: 'fullAccessTag' | 'notesAccess' | 'roAccess'; className: string }> = {
  full: { label: 'fullAccessTag', className: 'bg-green/15 text-green' },
  commentator: { label: 'notesAccess', className: 'bg-[var(--sticky-yellow-dark)] text-sticky-ink' },
  readonly: { label: 'roAccess', className: 'bg-[rgba(220,60,60,0.18)] text-red' },
};

export function Dashboard() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  // WHY: a ref, not a dep — the load runs on mount/route change, never on a language switch.
  const tRef = useRef(t);
  tRef.current = t;
  const { projects, setProjects, setCurrentProject, currentUser } = useAppStore(useShallow(s => ({ projects: s.projects, setProjects: s.setProjects, setCurrentProject: s.setCurrentProject, currentUser: s.currentUser })));
  const [newProjectName, setNewProjectName] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const renameInputRef = useRef<HTMLInputElement>(null);
  // WHY: a blur can arrive after Enter/Esc already closed the input — without
  // the guard the same commit would run twice (double PATCH, or a PATCH after
  // an explicit cancel).
  const renameDoneRef = useRef(false);

  useEffect(() => {
    if (!renamingId) return;
    renameInputRef.current?.focus();
    renameInputRef.current?.select();
  }, [renamingId]);

  useEffect(() => {
    setCurrentProject(null);
    apiClient.get('/projects').then(setProjects).catch((err) => {
      console.error('Failed to load projects', err);
      useAppStore.getState().showToast(tRef.current('failedToLoadProjects'), 'error');
    });
  }, [setProjects, setCurrentProject]);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;

    try {
      const project = await apiClient.post('/projects', { name: newProjectName });
      setProjects([project, ...projects]);
      setNewProjectName('');
      setIsCreating(false);
      navigate(docUrl(project.index_doc_id));
    } catch (err) {
      console.error('Failed to create project', err);
      useAppStore.getState().showToast(t('createProjectFailed'), 'error');
    }
  };

  const handleProjectClick = async (project: Project) => {
    // Fetch the fresh per-user last-accessed pointer right before navigating.
    // WHY: the store `projects` list may be stale — it was last fetched on a prior
    // Dashboard visit, before the user opened/changed a document. The async refetch
    // on mount may not have resolved when the user clicks, so we read the canonical
    // last_accessed_doc_id from the single-project endpoint to guarantee landing on
    // the document the user was actually in.
    const freshDocId = await apiClient.get(`/projects/${project.project_id}`)
      .then((data: { project: { last_accessed_doc_id?: string | null } }) =>
        data.project.last_accessed_doc_id || null)
      .catch(() => null);
    const targetDocId = freshDocId || project.last_accessed_doc_id || project.index_doc_id;
    if (targetDocId) navigate(docUrl(targetDocId));
  };

  const startRename = (e: React.MouseEvent, project: Project) => {
    e.stopPropagation();
    if (project.my_access !== 'full') return;
    renameDoneRef.current = false;
    setRenameValue(project.name);
    setRenamingId(project.project_id);
  };

  // Explicit try/catch (staged-archive handler shape, ReferencesPanel) — NOT
  // withOptimistic, which replaces the value wholesale with the API response.
  // The reconcile MERGES the PATCH row over the live row: the response is the
  // raw project record and lacks the list-endpoint joins (owner_name,
  // members_count) — replacing wholesale would drop them.
  const commitRename = async (project: Project) => {
    if (renameDoneRef.current) return;
    renameDoneRef.current = true;
    setRenamingId(null);
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === project.name || trimmed.length < 2) return;
    setProjects(projects.map(p => (p.project_id === project.project_id ? { ...p, name: trimmed } : p)));
    try {
      const updated: Project = await apiClient.patch(`/projects/${project.project_id}`, { name: trimmed });
      setProjects(useAppStore.getState().projects.map(p => (p.project_id === project.project_id ? { ...p, ...updated } : p)));
    } catch (err) {
      console.error('Failed to rename project', err);
      setProjects(useAppStore.getState().projects.map(p => (p.project_id === project.project_id ? { ...p, name: project.name } : p)));
      useAppStore.getState().showToast(t('renameProjectFailed'), 'error');
    }
  };

  const renderCard = (project: Project) => {
    const isRenaming = renamingId === project.project_id;
    const badge = project.owner_id !== currentUser?.user_id && project.my_access ? ACCESS_BADGE[project.my_access] : null;
    return (
      <div
        key={project.project_id}
        onClick={() => handleProjectClick(project)}
        className="group p-5 bg-surface border border-border-soft cursor-pointer transition-all duration-150 hover:border-accent hover:bg-surface2"
      >
        <div className="flex items-start justify-between gap-2">
          <h3 className={`text-base font-semibold text-text whitespace-nowrap${isRenaming ? ' flex-1 min-w-0 flex items-center' : ''}`}>
            {isRenaming ? (
              <FieldInput
                ref={renameInputRef}
                className="doc-rename-input project-rename-input"
                data-rename-input
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
                onKeyDown={(e) => {
                  e.stopPropagation();
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    void commitRename(project);
                  } else if (e.key === 'Escape') {
                    renameDoneRef.current = true;
                    setRenamingId(null);
                  }
                }}
                onBlur={() => { void commitRename(project); }}
                onClick={(e) => e.stopPropagation()}
              />
            ) : (
              project.name
            )}
            {badge && (
              <span data-access-badge={project.my_access} className={`${badge.className} pl-0.5 select-none`}>
                {` ${t(badge.label)}`}
              </span>
            )}
          </h3>
          {!isRenaming && project.my_access === 'full' && (
            <IconButton
              size="sm"
              title={t('rename')}
              className="shrink-0 opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
              onClick={(e) => startRename(e, project)}
            >
              <Pencil size={14} />
            </IconButton>
          )}
        </div>
        {project.owner_name && project.owner_id !== currentUser?.user_id && (
          <div className="text-xs text-text-dim mt-0.5">{project.owner_name}</div>
        )}
        {(project.members_count ?? 0) > 0 && (
          <div className="text-xs text-text-dim mt-0.5 flex items-center gap-1">
            <Users size={11} />
            <span>{project.members_count}</span>
          </div>
        )}
      </div>
    );
  };

  // WHY: split on owner_id, not is_owner_like — an admin with a member row is
  // owner-like but the project is still someone else's, so it is "shared".
  const ownProjects = projects.filter(p => p.owner_id === currentUser?.user_id);
  const sharedProjects = projects.filter(p => p.owner_id !== currentUser?.user_id);

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-[800px] mx-auto py-12 px-8">
      <section className="mb-10" data-section="own">
        <div className="flex items-center justify-between gap-2 pb-1.5 border-b border-border mb-3">
          {/* Text-only variant: the flex row above carries the rule (heading + create button). */}
          <CenterHeading as="h2" className="text-ui-md font-semibold text-text">{t('myProjects')}</CenterHeading>
          <Button variant="primary" onClick={() => setIsCreating(true)}>
            <FolderPlus size={16} />
            {t('newProject')}
          </Button>
        </div>

        {isCreating && (
          <div className="mb-3 p-5 bg-surface border border-border">
            <div className="font-medium mb-3 text-text">{t('createNewProject')}</div>
            <form onSubmit={handleCreateProject} className="flex gap-2">
              <FieldInput
                autoFocus
                type="text"
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                placeholder={t('projectPlaceholder')}
                className="flex-1"
              />
              <Button type="button" variant="ghost" onClick={() => setIsCreating(false)}>
                {t('cancel')}
              </Button>
              <Button type="submit" variant="primary">
                {t('create')}
              </Button>
            </form>
          </div>
        )}

        <div className="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-3">
          {ownProjects.map(renderCard)}
          {ownProjects.length === 0 && !isCreating && (
            <div className="col-span-full p-12 text-center border-2 border-dashed border-border text-text-dim">
              {t('noProjectsYet')}
            </div>
          )}
        </div>
      </section>

      {sharedProjects.length > 0 && (
        <section data-section="shared">
          <CenterHeading as="h2">{t('sharedProjects')}</CenterHeading>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-3">
            {sharedProjects.map(renderCard)}
          </div>
        </section>
      )}
    </div>
    </div>
  );
}
