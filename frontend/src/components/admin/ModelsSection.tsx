/** Admin: who may run which model (SYSTEM: model-access).
 *
 * One card per model of GET /api/admin/models (gateway roster ∪ granted ids):
 * chevron · id · default / "not in gateway" marks · Hidden · Tools · Private ·
 * Public selector. A gateway outage (in_gateway null) is one line above the list,
 * never a "not in gateway" mark on every row. The tier is derived from the
 * subjects: `hidden` → Hidden and `tools` → Tools (each exclusive, drops every
 * grant; disabled on the default model), `public` → Public, else Private. Expanding a card lists
 * every group (admin groups, then the read-only moderator groups) as a
 * checkbox — each one is that group's subject — greyed out while Hidden or Tools.
 * Every change shows at once, PUTs the model's FULL subject list and replaces
 * the row with the server's answer; a refusal (a moderator demoted meanwhile →
 * 422) restores the old subjects and renders under the row.
 * Cards drag-reorder (useSiblingDragReorder, the reference cards' mechanics);
 * a drop PUTs the full order, which every model picker follows.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { CenterHeading, FieldCheckbox, IconButton, SegmentedControl } from '../ui';
import {
  HIDDEN_SUBJECT, listAdminGroups, listAdminModels, putAdminModelOrder, putAdminModelSubjects,
  PUBLIC_SUBJECT, TOOLS_SUBJECT, type AdminGroup, type AdminModelAccess,
} from '../../api/admin';
import { rowDropLine, useSiblingDragReorder, type SiblingDragAdapter } from '../../hooks/useSiblingDragReorder';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';
import { groupLabel } from './GroupsSection';

type Tier = 'hidden' | 'tools' | 'private' | 'public';

const TIER_SUBJECTS = [PUBLIC_SUBJECT, HIDDEN_SUBJECT, TOOLS_SUBJECT];

/** A subject that is a group grant, not a tier marker. */
const isGroupGrant = (s: string) => !TIER_SUBJECTS.includes(s);

export function tierOf(subjects: string[]): Tier {
  if (subjects.includes(HIDDEN_SUBJECT)) return 'hidden';
  if (subjects.includes(TOOLS_SUBJECT)) return 'tools';
  return subjects.includes(PUBLIC_SUBJECT) ? 'public' : 'private';
}

/** The full subject list a tier change PUTs: Hidden and Tools drop every grant. */
export function subjectsForTier(subjects: string[], tier: Tier): string[] {
  const grants = subjects.filter(isGroupGrant);
  const byTier: Record<Tier, string[]> = {
    hidden: [HIDDEN_SUBJECT],
    tools: [TOOLS_SUBJECT],
    private: grants,
    public: [...grants, PUBLIC_SUBJECT],
  };
  return byTier[tier];
}

/** `rows` with `id` moved to just after `afterId` (null = top). */
export function moveAfter<T extends { id: string }>(rows: T[], id: string, afterId: string | null): T[] {
  const moved = rows.find(r => r.id === id);
  if (!moved) return rows;
  const rest = rows.filter(r => r.id !== id);
  const at = afterId === null ? 0 : rest.findIndex(r => r.id === afterId) + 1;
  return [...rest.slice(0, at), moved, ...rest.slice(at)];
}

export function ModelsSection() {
  const { t } = useTranslation();
  const [models, setModels] = useState<AdminModelAccess[] | null>(null);
  const [groups, setGroups] = useState<AdminGroup[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ id: string; text: string } | null>(null);
  const tRef = useRef(t);
  tRef.current = t;
  const modelsRef = useRef(models);
  modelsRef.current = models;
  const listRef = useRef<HTMLDivElement>(null);

  // WHY: refs, not state, inside the adapter — useSiblingDragReorder re-binds
  // its listeners whenever the adapter identity changes, so it is built once.
  const dragAdapter = useMemo<SiblingDragAdapter>(() => ({
    rowIdAttr: 'data-model-id',
    groupAttr: 'data-model-group',
    // Don't hijack the chevron, the tier selector or the group checkboxes.
    ignoreTarget: (el) => !!el.closest('button, input, label'),
    dropTarget: (row, e, candidate) => {
      const rowId = row.getAttribute('data-model-id')!;
      if (rowId === candidate.id) return undefined;
      const others = (modelsRef.current ?? []).filter(m => m.id !== candidate.id);
      const idx = others.findIndex(m => m.id === rowId);
      if (idx === -1) return undefined;
      const rect = row.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      const afterId = before ? (idx > 0 ? others[idx - 1].id : null) : rowId;
      return { parentId: null, afterId, ...rowDropLine(rect, before) };
    },
    commit: (id, target) => {
      const prev = modelsRef.current;
      if (!prev) return;
      const next = moveAfter(prev, id, target.afterId);
      setModels(next);
      putAdminModelOrder(next.map(m => m.id)).catch(() => {
        setModels(prev); // rollback
        useAppStore.getState().showToast(tRef.current('adminModelOrderFailed'), 'error');
      });
    },
  }), []);
  useSiblingDragReorder(listRef, models !== null, dragAdapter);

  const reload = useCallback(async () => {
    try {
      const [m, g] = await Promise.all([listAdminModels(), listAdminGroups()]);
      setModels(m);
      setGroups(g);
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      useAppStore.getState().showToast(tRef.current('adminListLoadFailed'), 'error');
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const setRowSubjects = (id: string, subjects: string[]) =>
    setModels(prev => prev?.map(m => (m.id === id ? { ...m, subjects } : m)) ?? prev);

  // WHY: optimistic — the new state shows on click and the server's answer
  // replaces it; a refusal restores the old subjects and renders under the row.
  // A busy row ignores clicks instead of dimming, so the control does not blink.
  const saveSubjects = async (model: AdminModelAccess, subjects: string[]) => {
    if (busy) return;
    setBusy(model.id);
    setMsg(null);
    setRowSubjects(model.id, subjects);
    try {
      const saved = await putAdminModelSubjects(model.id, subjects);
      setRowSubjects(model.id, saved.subjects);
    } catch (err: unknown) {
      setRowSubjects(model.id, model.subjects);
      setMsg({ id: model.id, text: serverRefusalDetail(err) ?? t('adminModelSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

  const toggleSubject = (model: AdminModelAccess, subject: string, on: boolean) => saveSubjects(
    model, on ? [...model.subjects, subject] : model.subjects.filter(s => s !== subject),
  );

  return (
    <>
      {/* Variant of the admin heading: mb-1 — the hint hugs the rule. */}
      <CenterHeading className="text-ui-md font-semibold text-text pb-1.5 border-b border-border mb-1">{t('adminModelAccess')}</CenterHeading>
      <p className="text-xs text-text-dim mb-3">{t('adminModelAccessHint')}</p>
      {loadFailed && <p className="text-xs text-red mb-2">{t('adminListLoadFailed')}</p>}
      {models?.some(m => m.in_gateway === null) && (
        <p className="text-xs text-red mb-2">{t('adminModelGatewayUnavailable')}</p>
      )}
      <div ref={listRef} className="flex flex-col gap-0.5">
        {(models ?? []).map(model => {
          const open = expanded === model.id;
          const tier = tierOf(model.subjects);
          const groupCount = model.subjects.filter(isGroupGrant).length;
          return (
            <section
              key={model.id}
              data-model-id={model.id}
              data-model-group=""
              className={`hover:bg-surface2 transition-colors duration-150 ${open ? 'bg-surface2' : 'bg-surface'}`}
            >
              <div className="py-2.5 px-3.5 flex items-center gap-3">
                <IconButton size="sm" onClick={() => setExpanded(open ? null : model.id)}>
                  {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </IconButton>
                <span className={`text-sm font-medium truncate ${model.in_gateway === false ? 'text-text-dim' : 'text-text'}`}>
                  {model.id}
                </span>
                {model.is_default && (
                  <span className="text-xs text-text-dim whitespace-nowrap">{t('adminModelDefault')}</span>
                )}
                {model.in_gateway === false && (
                  <span className="text-xs text-red whitespace-nowrap">{t('adminModelNotInGateway')}</span>
                )}
                <span className="flex-1" />
                {groupCount > 0 && (
                  <span className="text-xs text-text-dim whitespace-nowrap">{t('adminModelGroupCount', { count: groupCount })}</span>
                )}
                <SegmentedControl
                  ariaLabel={t('adminModelTier')}
                  value={tier}
                  onChange={next => void saveSubjects(model, subjectsForTier(model.subjects, next as Tier))}
                  options={[
                    { value: 'hidden', label: t('adminModelHidden'), disabled: model.is_default },
                    { value: 'tools', label: t('adminModelTools'), disabled: model.is_default },
                    { value: 'private', label: t('adminModelPrivate') },
                    { value: 'public', label: t('adminModelPublic') },
                  ]}
                />
              </div>
              {open && (
                <div className="px-3.5 pb-3 pl-12 flex flex-col gap-1.5">
                  {groups.length === 0 && <p className="text-xs text-text-dim">{t('adminGroupsEmpty')}</p>}
                  {groups.map(g => (
                    <FieldCheckbox
                      key={g.subject}
                      checked={model.subjects.includes(g.subject)}
                      onChange={on => void toggleSubject(model, g.subject, on)}
                      label={groupLabel(g, t)}
                      disabled={busy === model.id || tier === 'hidden' || tier === 'tools'}
                    />
                  ))}
                </div>
              )}
              {msg?.id === model.id && <p className="text-xs px-3.5 pb-2 text-red">{msg.text}</p>}
            </section>
          );
        })}
      </div>
    </>
  );
}
