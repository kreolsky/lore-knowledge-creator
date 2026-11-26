---
name: tasks
description: Use when the user asks to work the project's task backlog or task loop, or asks what to do next, or a task is named with its status, or answers a task's question or sends a task to work. «разгреби бэклог», «возьми задачу», «что дальше по задачам», «сделай задачу», «закрой задачу», «новая задача», «создай задачу», «ответ по задаче», «в работу», «верни задачу»; work the backlog, take a task, next task, task loop, claim a task, add a task, answer a task's question, send a task to work.
tools:
  - find_documents
  - next_task
  - claim_task
  - set_task_status
---

# Tasks

This project's tasks are documents whose YAML header says `type: task`. The
convention around them — statuses, priorities, dependencies — is defined by a
schema document the project owns; this is the working method on top.

## Before the first task

Read the schema document (`next_task` returns its `schema_id`): its header
defines the statuses and their order, its body carries THIS project's rules —
what the log heading is called, what "done" means, branch naming, which
repository. The schema body OVERRIDES anything below that contradicts it.
The header's `priority` list runs from most urgent to least: its FIRST value
is served first. The `ask` status (`next_task` returns it; null when the
schema has none) is where a task waits for the person's answer.

Writing the log and creating tasks needs the `main-edit` skill; reading the
parent and neighbours needs `main-read` — load them. If `main-edit` is hidden
from you, do not hand a task back without its report: say so in your reply.

## The loop

1. `next_task(agent_name)` — first look at `in_progress`: an entry whose
   `assignee` is your name is your own unfinished work — resume it (renew
   with `claim_task`) before taking a new task. Otherwise `task` is the next
   available one (ready, or a claim whose lease expired, with every
   dependency closed). Null task = nothing to do; say so. Tasks listed in
   `reserved` are assigned to someone else — leave them. A task on the `ask`
   status waits for the person and is offered to nobody.
   A `blocked` entry whose reason is `dependency not visible` or `dependency
   unresolvable` will never unblock on its own (a broken link, or a target out
   of your reach) — name those tasks to the person in your reply.
2. `claim_task(document_id, agent_name)` — take it. On 409 `already_claimed`
   another agent was faster: call `next_task` again, do not fight for the
   task. A claim lasts 4 hours; if the work outlives it, call `claim_task`
   again with the same `agent_name` to renew.
3. Read before working: the task itself, its parent (the story it belongs
   to), and every neighbour `get_links` names on it.
4. Work. Finish with the report and `set_task_status(document_id, <review
   status>, agent_name)` (below); the review status is the one that is neither
   the initial, ready, ask, claimed nor a closed one. A question you cannot
   resolve parks the task instead (see Ambiguity). Never close on your own
   initiative — review is the hand-back to the person, and closing is theirs:
   when the person explicitly asks you to close that task, call
   `set_task_status` with a closed status. «Отмени» / «cancel» on the
   person's word is the closed status `dropped` (when the schema lists it),
   with the reason written into the log first. Never change `status:` by
   editing the header.

A 409 `open_subtasks` or `already_claimed` from `set_task_status` is reported
to the person, not worked around. A 500 after a close is retried once with
the same status and the same `note` — it completes the move into the archive,
and the note must ride again or the bell is lost.

## Reading a task

The task states intent and limits. The code is the truth on HOW things
currently work. A contradiction between task and code is REPORTED in the log,
never resolved silently in either direction — do not "fix" the code to match
the task beyond the task's stated scope, and do not reinterpret the task to
match the code.

## Writing back

The report goes into the task's body, under the log heading the schema body
names (the generic default is `## Log`). Write intent, not description:

- what was done, and the commit / PR / branch that carries it;
- what was NOT proven (untested paths, assumptions you had to make);
- open questions for the person reviewing.

`set_task_status` removes the `claimed_until` line. A hand-back to the review
status keeps `assignee` — it records who did the work, and a task reopened by
hand returns to you. A return to the initial, ready or ask status removes
`assignee` too: the task is free, and any agent may take it. A hand-back that
is not a question carries no `note` — the note is the bell, and it rings for
a question only.

Nothing inferable from the code is copied into the project — a log records
decisions and evidence, not the source. A spec you wrote along the way
becomes its own document, linked from the task header as
`spec: "[title](doc:<id>)"` — never pasted into the task.

## Ambiguity

Do the unambiguous part first. Then write the question into the log and park
the task: `set_task_status(document_id, <the ask status>, agent_name,
note=<the question>)`. The server puts the question in a system note on the
task with a link to it and marks it unread for the person; the task is free
while it waits, and any agent continues it after the answer. On
`note: "created"` take the next task; on `note: "failed"` repeat the same
call once, then tell the person in the reply.

No `ask` status in the schema (`next_task` returned null): set the review
status instead and ask in your reply. None or several review statuses:
leave the task claimed, write the question into the log, ask in your reply.
Never guess on scope to keep a task moving.

## The person answers

An answer about a task that the person gives in this chat is written into
that task's log first — the task is the record, the chat is not. Then, only
on the person's word, `set_task_status(document_id, <the ready status>,
agent_name)` sends the task back to work; any agent may claim it, the one
that asked included. A task the person names by words is found by its title
among the tasks `find_documents` lists (`where: {type: task}`); several
candidates — ask which one.

## Discovered work

Work found while inside a task does NOT go into that task (no scope creep).
Create a new task document under the same parent, header `status:` = the
`initial` status `next_task` returned — nobody does it without the person's
word — and name it in your reply. If it blocks the task, link it there with
`depends` (each entry a QUOTED Lore link or a bare document id — an
unquoted link breaks the YAML):

    depends:
      - "[Fix the importer](doc:<id>)"

## Authoring

Two statuses take a new task, by who sent it to work. A reminder («надо бы»,
«запомни») — the person's own, not work yet — and work you found by yourself
get the initial status: a draft, thought through in Lore with the person
before it becomes ready, and agents do not take it. A formulated intent the
person sends to work («делай таску») gets the ready status; the plan is
written and discussed from there. A draft moves to ready only on the
person's word.

An `assignee: <agent name>` on a resting task (initial, ask or ready)
reserves it: other agents skip it. A return to ask clears it, so after your
question the person repeats the assignment.

A task document (the header is the contract; the body states intent):

    ---
    type: task
    status: <the initial or the ready status>
    priority: <a value from the schema's priority list, optional>
    assignee: <an agent's name, optional — reserves the task for it>
    depends:
      - "[title](doc:<id>)"
    ---
    # <title>

    <intent and limits — what "done" means, what is out of scope>

A schema document (one per project, authored by the team):

    ---
    type: schema
    schema_for: task
    statuses: [draft, ask, todo, doing, review, done, dropped]
    ready: todo
    claimed: doing
    closed: [done, dropped]
    initial: draft
    ask: ask
    priority: [urgent, high, normal, low]
    archive: "[DONE](doc:<id>)"  # optional: closed top-level tasks move here
    ---
    # Tasks

    <this project's rules: log heading and format, review meaning,
    branch naming, definition of done, which repository>
