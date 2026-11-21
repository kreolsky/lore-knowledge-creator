/** The agent's standing task list, docked above the AI composer — ported from dsh ui-conversation's TodoPanel; reads the chat-store `todos` publication (SYSTEM: dsh-conversation). */

import { useState } from 'react';
import { ChevronDown, ChevronUp, Circle, CircleCheck, CircleDot, ListChecks } from 'lucide-react';
import { useChatStore } from '../../store/chat-store';
import type { TodoItem } from '../../store/chat-store/conversation-feed';
import { useTranslation } from '../../i18n';

const STATUS_ICON: Record<TodoItem['status'], typeof Circle> = {
  pending: Circle,
  in_progress: CircleDot,
  completed: CircleCheck,
};

const STATUS_TONE: Record<TodoItem['status'], string> = {
  pending: 'text-text-dim',
  in_progress: 'text-accent',
  completed: 'text-green',
};

const STATUS_LABEL = {
  pending: 'todoStatusPending',
  in_progress: 'todoStatusInProgress',
  completed: 'todoStatusCompleted',
} as const;

/**
 * Collapsible checklist: a header with the done count (collapsed, it also
 * names the item in progress), expanded the whole list with a status mark per
 * row. Renders nothing when the turn has no list — empty is not an empty box.
 */
export function TodoDock() {
  const todos = useChatStore(s => s.todos);
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(true);
  if (!todos || todos.length === 0) return null;
  const done = todos.filter(item => item.status === 'completed').length;
  const current = todos.find(item => item.status === 'in_progress');
  return (
    <section
      className="border border-border bg-surface2 text-ui-sm text-text"
      aria-label={t('todoTitle')}
      data-testid="todo-dock"
    >
      {/* Full-width disclosure header — the closed-prop Button cannot express this layout. */}
      {/* eslint-disable-next-line react/forbid-elements */}
      <button
        type="button"
        onClick={() => setCollapsed(v => !v)}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left cursor-pointer bg-transparent"
      >
        <ListChecks size={13} className="shrink-0 text-text-muted" />
        <span className="shrink-0 font-medium">{t('todoTitle')}</span>
        <span className="shrink-0 text-text-muted">{t('todoProgress', { done, total: todos.length })}</span>
        {collapsed && current && (
          <span className="min-w-0 truncate text-text-muted">· {current.content}</span>
        )}
        <span className="ml-auto shrink-0 text-text-muted">
          {collapsed ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        </span>
      </button>
      {!collapsed && (
        // WHY capped: a long plan must not push the composer off screen.
        <ul className="max-h-[min(240px,40vh)] overflow-auto px-2 pb-1.5">
          {todos.map((item, index) => {
            const Icon = STATUS_ICON[item.status];
            return (
              <li key={`${index}:${item.content}`} className="flex items-start gap-1.5 py-0.5">
                <Icon
                  size={13}
                  className={`mt-0.5 shrink-0 ${STATUS_TONE[item.status]}`}
                  role="img"
                  aria-label={t(STATUS_LABEL[item.status])}
                />
                <span className={item.status === 'completed' ? 'text-text-muted line-through' : undefined}>
                  {item.content}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
