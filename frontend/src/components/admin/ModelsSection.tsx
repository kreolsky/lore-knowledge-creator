/** Admin: who may run which model (SYSTEM: model-access).
 *
 * One card per model of GET /api/admin/models (gateway roster ∪ granted ids):
 * chevron · id · default / "not in gateway" marks · Hidden · Private · Public
 * selector. A gateway outage (in_gateway null) is one line above the list,
 * never a "not in gateway" mark on every row. The tier is derived from the
 * subjects: `hidden` → Hidden (exclusive, drops every grant; disabled on the
 * default model), `public` → Public, else Private. Expanding a card lists
 * every group (admin groups, then the read-only moderator groups) as a
 * checkbox — each one is that group's subject — greyed out while Hidden.
 * Every change shows at once, PUTs the model's FULL subject list and replaces
 * the row with the server's answer; a refusal (a moderator demoted meanwhile →
 * 422) restores the old subjects and renders under the row.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { CenterHeading, FieldCheckbox, IconButton, SegmentedControl } from '../ui';
import {
  HIDDEN_SUBJECT, listAdminGroups, listAdminModels, putAdminModelSubjects, PUBLIC_SUBJECT,
  type AdminGroup, type AdminModelAccess,
} from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';
import { groupLabel } from './GroupsSection';

type Tier = 'hidden' | 'private' | 'public';

function tierOf(subjects: string[]): Tier {
  if (subjects.includes(HIDDEN_SUBJECT)) return 'hidden';
  return subjects.includes(PUBLIC_SUBJECT) ? 'public' : 'private';
}

/** The full subject list a tier change PUTs: Hidden drops every grant. */
function subjectsForTier(subjects: string[], tier: Tier): string[] {
  const grants = subjects.filter(s => s !== PUBLIC_SUBJECT && s !== HIDDEN_SUBJECT);
  const byTier: Record<Tier, string[]> = {
    hidden: [HIDDEN_SUBJECT],
    private: grants,
    public: [...grants, PUBLIC_SUBJECT],
  };
  return byTier[tier];
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
      <div className="flex flex-col gap-0.5">
        {(models ?? []).map(model => {
          const open = expanded === model.id;
          const tier = tierOf(model.subjects);
          const groupCount = model.subjects.filter(s => s !== PUBLIC_SUBJECT && s !== HIDDEN_SUBJECT).length;
          return (
            <section
              key={model.id}
              data-model-id={model.id}
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
                      disabled={busy === model.id || tier === 'hidden'}
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
