/** Admin: full project list — per-project card with rename, delete, public
 * toggle and members. All mutation handlers stay in AdminPage; each card owns
 * only its inline edit state, so one project's rename cannot leak into another.
 */

import { useState, useRef } from 'react';
import { Link } from 'react-router-dom';
import { Trash2, ChevronDown, ChevronRight, ExternalLink, Pencil, Users } from 'lucide-react';
import { Button, FieldInput, IconButton } from '../ui';
import { useTranslation } from '../../i18n';
import { AccessLevel, User, Project } from '../../types';
import { ProjectMemberList } from './ProjectMemberList';

interface ProjectCardProps {
  project: Project;
  users: User[];
  onRename: (project: Project, newName: string) => void;
  onDelete: (project: Project, confirmValue: string) => void;
  onSetAccess: (projectId: string, userId: string, access: AccessLevel | null) => void;
  onTransferOwner: (projectId: string, userId: string) => Promise<boolean>;
  onTogglePublic: (project: Project) => void;
  onToggleExpand: (projectId: string) => void;
  expanded: boolean;
  members: Record<string, string>;
}

interface ProjectsTabProps {
  projects: Project[];
  users: User[];
  onRename: (project: Project, newName: string) => void;
  onDelete: (project: Project, confirmValue: string) => void;
  onSetAccess: (projectId: string, userId: string, access: AccessLevel | null) => void;
  onTransferOwner: (projectId: string, userId: string) => Promise<boolean>;
  onTogglePublic: (project: Project) => void;
  onToggleExpand: (projectId: string) => void;
  expandedProject: string | null;
  projectMembers: Record<string, Record<string, string>>;
}

export function ProjectsTab({ projects, expandedProject, projectMembers, ...cardProps }: ProjectsTabProps) {
  return (
    <div className="flex flex-col gap-0.5">
      {projects.map(project => (
        <ProjectCard
          key={project.project_id}
          project={project}
          expanded={expandedProject === project.project_id}
          members={projectMembers[project.project_id] || {}}
          {...cardProps}
        />
      ))}
    </div>
  );
}

function ProjectCard({
  project,
  users,
  onRename,
  onDelete,
  onSetAccess,
  onTransferOwner,
  onTogglePublic,
  onToggleExpand,
  expanded,
  members,
}: ProjectCardProps) {
  const { t } = useTranslation();

  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const renameInputRef = useRef<HTMLInputElement>(null);

  const [deleting, setDeleting] = useState(false);
  const [deleteConfirmValue, setDeleteConfirmValue] = useState('');
  const deleteInputRef = useRef<HTMLInputElement>(null);

  const startRename = () => {
    setRenaming(true);
    setRenameValue(project.name);
    setTimeout(() => renameInputRef.current?.focus(), 50);
  };

  const commitRename = () => {
    const trimmed = renameValue.trim();
    setRenaming(false);
    if (!trimmed || trimmed === project.name) return;
    onRename(project, trimmed);
  };

  const startDelete = () => {
    setDeleting(true);
    setDeleteConfirmValue('');
    setTimeout(() => deleteInputRef.current?.focus(), 50);
  };

  const commitDelete = () => {
    if (deleteConfirmValue !== project.name) return;
    onDelete(project, deleteConfirmValue);
    setDeleting(false);
    setDeleteConfirmValue('');
  };

  return (
    // Borderless pill with the references' hover tone (ListPill: hover =
    // surface2); an expanded card holds that tone while open.
    <section className={`hover:bg-surface2 transition-colors duration-150 ${expanded ? 'bg-surface2' : 'bg-surface'}`}>
      <div className="py-2.5 px-3.5 flex items-center gap-3">
        <IconButton
          size="sm"
          onClick={() => onToggleExpand(project.project_id)}
        >
          {expanded
            ? <ChevronDown size={14} />
            : <ChevronRight size={14} />}
        </IconButton>

        {/* Rename edits IN PLACE (references' contract, see .ref-rename-input):
         * the title span stays, its text becomes a borderless input with the
         * accent underline; the members chip stays where it was. The owner is
         * NOT chipped here — the expanded member list's pinned owner row is
         * the single owner surface. */}
        {/* Chips are separate flex children spaced by the parent's `gap-2`,
         * exactly like UsersTab's RoleChip — no inline `&nbsp;|` that would
         * add to the gap on one item only. */}
        <span
          className="flex-1 text-sm text-text font-medium flex gap-2 items-baseline min-w-0"
          onDoubleClick={() => { if (!renaming) startRename(); }}
        >
          {renaming ? (
            <FieldInput
              ref={renameInputRef}
              className="doc-rename-input admin-rename-input min-w-0"
              data-rename-input
              value={renameValue}
              onChange={e => setRenameValue(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') commitRename();
                if (e.key === 'Escape') setRenaming(false);
              }}
              onBlur={() => commitRename()}
            />
          ) : (
            <span className="truncate">{project.name}</span>
          )}
          {(project.members_count ?? 0) > 0 && (
            <span className="text-xs text-text-dim font-normal whitespace-nowrap">
              | <Users size={11} className="inline align-[-1px]" /> {project.members_count}
            </span>
          )}
        </span>

        {/* Accent fill only while PUBLIC: the project being open to everyone
         * must read at a glance from the list; private stays the quiet ghost. */}
        <Button
          variant={project.is_public ? 'primary' : 'ghost'}
          size="sm"
          onClick={() => onTogglePublic(project)}
        >
          {project.is_public ? t('publicLabel') : t('privateLabel')}
        </Button>

        <IconButton
          size="sm"
          title={t('rename')}
          onClick={() => startRename()}
        >
          <Pencil size={14} />
        </IconButton>

        <IconButton
          size="sm"
          danger
          title={t('deleteProject')}
          onClick={() => startDelete()}
        >
          <Trash2 size={14} />
        </IconButton>

        {/* Sized like IconButton sm so the card is as tall as a user's/skill's. */}
        {/* No my_access = the viewer cannot enter (owner/member/public none of
         * those) — render NO link, just the disabled look with the reason;
         * a navigable link would always land on the error page. */}
        {project.my_access ? (
          <Link
            to={`/projects/${project.project_id}`}
            className="w-[22px] h-[22px] border-none bg-transparent text-text-muted flex items-center justify-center transition-all duration-150 hover:bg-surface3 hover:text-text"
            title={t('openProject')}
          >
            <ExternalLink size={14} />
          </Link>
        ) : (
          <span
            className="w-[22px] h-[22px] border-none bg-transparent text-text-muted opacity-40 flex items-center justify-center cursor-not-allowed"
            title={t('noProjectAccess')}
          >
            <ExternalLink size={14} />
          </span>
        )}
      </div>

      {deleting && (
        <div className="border-t border-border py-2 px-3.5 flex items-center gap-2 bg-surface2">
          <span className="text-xs text-text-dim whitespace-nowrap">
            {t('typeToConfirmProject', { projectName: project.name })}
          </span>
          <FieldInput
            ref={deleteInputRef}
            className="flex-1 text-ui-base"
            value={deleteConfirmValue}
            onChange={e => setDeleteConfirmValue(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') commitDelete();
              if (e.key === 'Escape') { setDeleting(false); setDeleteConfirmValue(''); }
            }}
          />
          <Button
            variant="primary"
            size="lg"
            disabled={deleteConfirmValue !== project.name}
            onClick={() => commitDelete()}
          >
            {t('delete')}
          </Button>
          <Button
            variant="ghost"
            size="lg"
            onClick={() => { setDeleting(false); setDeleteConfirmValue(''); }}
          >
            {t('cancel')}
          </Button>
        </div>
      )}

      {expanded && (
        <ProjectMemberList
          members={members}
          users={users}
          ownerId={project.owner_id}
          ownerName={project.owner_name ?? null}
          projectId={project.project_id}
          onSetAccess={onSetAccess}
          onTransferOwner={onTransferOwner}
        />
      )}
    </section>
  );
}
