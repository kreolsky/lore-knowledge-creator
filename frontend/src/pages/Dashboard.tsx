/** Project list split into own / shared sections, create-project form, and the card edit modal. Rendered at /projects route. */
// ARCH: Extracted from App.tsx — page component with its own data fetching.

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import type { AccessLevel, Project } from '../types';
import { docUrl } from '../utils/routing';
import { FolderPlus, Pencil, Users } from 'lucide-react';
import { Button, IconButton, Modal } from '../components/ui';
// WHY: direct path, not the '../components/ui' barrel — the Dashboard tests
// mock the barrel with only the interactive components.
import { CenterHeading } from '../components/ui/CenterHeading';
import { ProjectForm } from './ProjectForm';
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
  const [isCreating, setIsCreating] = useState(false);
  // The project whose edit modal is open (pencil, my_access === 'full' only).
  const [editing, setEditing] = useState<Project | null>(null);

  useEffect(() => {
    setCurrentProject(null);
    apiClient.get('/projects').then(setProjects).catch((err) => {
      console.error('Failed to load projects', err);
      useAppStore.getState().showToast(tRef.current('failedToLoadProjects'), 'error');
    });
  }, [setProjects, setCurrentProject]);

  const handleCreateProject = async (name: string, description: string | null) => {
    try {
      const project = await apiClient.post('/projects', { name, description });
      setProjects([project, ...projects]);
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

  // Explicit try/catch (staged-archive handler shape, ReferencesPanel) — NOT
  // withOptimistic, which replaces the value wholesale with the API response.
  // The reconcile MERGES the PATCH row over the live row: the response is the
  // raw project record and lacks the list-endpoint joins (owner_name,
  // members_count) — replacing wholesale would drop them.
  const commitEdit = async (name: string, description: string | null) => {
    if (!editing) return;
    const project = editing;
    setEditing(null);
    if (name === project.name && description === (project.description ?? null)) return;
    setProjects(projects.map(p => (p.project_id === project.project_id ? { ...p, name, description } : p)));
    try {
      const updated: Project = await apiClient.patch(`/projects/${project.project_id}`, { name, description });
      setProjects(useAppStore.getState().projects.map(p => (p.project_id === project.project_id ? { ...p, ...updated } : p)));
    } catch (err) {
      console.error('Failed to update project', err);
      setProjects(useAppStore.getState().projects.map(p => (p.project_id === project.project_id ? { ...p, name: project.name, description: project.description } : p)));
      useAppStore.getState().showToast(t('updateProjectFailed'), 'error');
    }
  };

  const renderCard = (project: Project) => {
    const badge = project.owner_id !== currentUser?.user_id && project.my_access ? ACCESS_BADGE[project.my_access] : null;
    const isOwn = project.owner_id === currentUser?.user_id;
    const members = project.members_count ?? 0;
    const showOwner = !isOwn && !!project.owner_name;
    return (
      <div
        key={project.project_id}
        onClick={() => handleProjectClick(project)}
        className="group p-5 bg-surface border border-border-soft cursor-pointer transition-all duration-150 hover:border-accent hover:bg-surface2"
      >
        <div className="flex items-start justify-between gap-2">
          <h3 className="text-base font-semibold text-text whitespace-nowrap">
            {project.name}
            {badge && (
              <span data-access-badge={project.my_access} className={`${badge.className} pl-0.5 select-none`}>
                {` ${t(badge.label)}`}
              </span>
            )}
          </h3>
          {project.my_access === 'full' && (
            <IconButton
              size="sm"
              title={t('editProject')}
              className="shrink-0 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100"
              onClick={(e) => {
                e.stopPropagation();
                setEditing(project);
              }}
            >
              <Pencil size={14} />
            </IconButton>
          )}
        </div>
        {project.description && (
          <div data-description className="text-sm text-text-dim mt-1 line-clamp-5 whitespace-pre-line">
            {project.description}
          </div>
        )}
        {(showOwner || members > 0) && (
          <div data-meta className="text-xs text-text-dim mt-0.5 flex items-center gap-1">
            {showOwner && <span className="truncate">{project.owner_name}</span>}
            {showOwner && members > 0 && <span aria-hidden="true">|</span>}
            {members > 0 && (
              <>
                <Users size={11} />
                <span>{members}</span>
              </>
            )}
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
            <ProjectForm
              initialName=""
              initialDescription=""
              submitLabel={t('create')}
              onSubmit={(name, description) => { void handleCreateProject(name, description); }}
              onCancel={() => setIsCreating(false)}
            />
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

      <Modal open={editing !== null} onClose={() => setEditing(null)} title={t('editProject')} closeLabel={t('close')}>
        {editing && (
          <ProjectForm
            initialName={editing.name}
            initialDescription={editing.description ?? ''}
            submitLabel={t('save')}
            onSubmit={(name, description) => { void commitEdit(name, description); }}
            onCancel={() => setEditing(null)}
          />
        )}
      </Modal>
    </div>
    </div>
  );
}
