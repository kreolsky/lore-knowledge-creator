---
name: tasks
description: Use when the user asks to work the project's task backlog or task loop, or asks what to do next, or a task is named with its status. «разгреби бэклог», «возьми задачу», «что дальше по задачам», «сделай задачу», «закрой задачу», «новая задача», «создай задачу»; work the backlog, take a task, next task, task loop, claim a task, add a task.
tools:
  - find_documents
  - next_task
  - claim_task
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
is served first.

## The loop

1. `next_task` — the next available task (ready, or a claim whose lease
   expired, with every dependency closed). Null task = nothing to do; say so.
   A `blocked` entry whose reason is `dependency not visible` or `dependency
   unresolvable` will never unblock on its own (a broken link, or a target out
   of your reach) — name those tasks to the person in your reply.
2. `claim_task(document_id, agent_name)` — take it. On 409 `already_claimed`
   another agent was faster: call `next_task` again, do not fight for the
   task. A claim lasts 4 hours; if the work outlives it, call `claim_task`
   again with the same `agent_name` to renew.
3. Read before working: the task itself, its parent (the story it belongs
   to), and every neighbour `get_links` names on it.
4. Work. Finish with the report and the review status (below). Never set a
   closed status on your own initiative — review is the hand-back to the
   person, and closing is theirs: set it only when the person explicitly asks
   you to close that task.

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

When you set the review status, delete the `claimed_until` line from the
header and keep `assignee` — it records who did the work, while a leftover
lease would hand the task back to you if someone reopens it by hand.

Nothing inferable from the code is copied into the project — a log records
decisions and evidence, not the source. A spec you wrote along the way
becomes its own document, linked from the task header as
`spec: "[title](doc:<id>)"` — never pasted into the task.

## Ambiguity

Do the unambiguous part. Write the question into the log, set the review
status, and stop — never guess on scope to keep a task moving.

## Discovered work

Work found while inside a task does NOT go into that task (no scope creep).
Create a new task document under the same parent, header `status:` = the
`initial` status `next_task` returned, and link it from the blocking task
with `depends` (each entry a QUOTED Lore link or a bare document id — an
unquoted link breaks the YAML):

    depends:
      - "[Fix the importer](doc:<id>)"

## Authoring

A task document (the header is the contract; the body states intent):

    ---
    type: task
    status: <the schema's initial status>
    priority: <a value from the schema's priority list, optional>
    depends:
      - "[title](doc:<id>)"
    ---
    # <title>

    <intent and limits — what "done" means, what is out of scope>

A schema document (one per project, authored by the team):

    ---
    type: schema
    schema_for: task
    statuses: [todo, doing, review, done]
    ready: todo
    claimed: doing
    closed: [done]
    initial: todo
    priority: [urgent, high, normal, low]
    ---
    # Tasks

    <this project's rules: log heading and format, review meaning,
    branch naming, definition of done, which repository>
