/** Admin: who may run which model (SYSTEM: model-access).
 *
 * One card per model of GET /api/admin/models (gateway roster ∪ granted ids):
 * chevron · id · default / "not in gateway" marks · Public checkbox. A
 * gateway outage (in_gateway null) is one line above the list, never a
 * "not in gateway" mark on every row. The
 * Public checkbox is the `public` subject; expanding a card lists every group
 * (admin groups, then the read-only moderator groups) as a checkbox — each
 * one is that group's subject. Every toggle PUTs the model's FULL subject
 * list at once and replaces the row with the server's answer; a refusal
 * (a moderator demoted meanwhile → 422) renders under the row.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { CenterHeading, FieldCheckbox, IconButton } from '../ui';
import {
  listAdminGroups, listAdminModels, putAdminModelSubjects, PUBLIC_SUBJECT,
  type AdminGroup, type AdminModelAccess,
} from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';
import { groupLabel } from './GroupsSection';

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

  const toggleSubject = async (model: AdminModelAccess, subject: string, on: boolean) => {
    const subjects = on
      ? [...model.subjects, subject]
      : model.subjects.filter(s => s !== subject);
    setBusy(model.id);
    setMsg(null);
    try {
      const saved = await putAdminModelSubjects(model.id, subjects);
      setModels(prev => prev?.map(m => (m.id === model.id ? { ...m, subjects: saved.subjects } : m)) ?? prev);
    } catch (err: unknown) {
      setMsg({ id: model.id, text: serverRefusalDetail(err) ?? t('adminModelSaveFailed') });
    } finally {
      setBusy(null);
    }
  };

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
          const groupCount = model.subjects.filter(s => s !== PUBLIC_SUBJECT).length;
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
                <FieldCheckbox
                  checked={model.subjects.includes(PUBLIC_SUBJECT)}
                  onChange={on => void toggleSubject(model, PUBLIC_SUBJECT, on)}
                  label={t('adminModelPublic')}
                  disabled={busy === model.id}
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
                      disabled={busy === model.id}
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
