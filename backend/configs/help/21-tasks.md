# Tasks

A task is an ordinary document whose YAML header says `type: task`. Agents — the built-in one and an external one on a key — take tasks, work them, report back and hand them to you for review. You decide what goes to work and what gets closed.

## The schema

Each project has one schema document: its header says `type: schema` and `schema_for: task` and lists the statuses and which of them plays which role.

```yaml
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
archive: "[DONE](doc:<id>)"
---
# Tasks

The log heading is "## Log". Done means merged and checked on the test server.
```

- `ready` — waiting to be taken; `claimed` — an agent is working on it; `closed` — finished.
- `initial` (optional) — a draft; `ask` (optional) — the task waits for your answer.
- The status that is none of these is the review status, where an agent hands the task back to you.
- `priority` runs from the most urgent to the least: the first value is served first.
- `archive` (optional) — the document where closed top-level tasks move.

The schema's body holds the project's own rules: what the log heading is called, what "done" means, branch naming, which repository. An agent reads it before its first task, and the body overrides its generic method.

A task document:

```yaml
---
type: task
status: todo
priority: high
depends:
  - "[Fix the importer](doc:<id>)"
---
# Export tables to CSV

What "done" means, what is out of scope.
```

`depends` lists tasks that must be closed first; each entry is a quoted link.

## How an agent works

1. Takes the next ready task whose dependencies are closed, and claims it. A claim lasts 4 hours and the agent renews it while it works.
2. Reads the task, its parent and its linked neighbours, and does the work.
3. Writes a report under the log heading: what was done, what was not proven, open questions.
4. Moves the task to the review status.

An agent never closes a task on its own; it closes one only when you ask.

## Questions

When an agent hits a question it cannot resolve, it writes the question into the log and moves the task to `ask`. A system note with the question and a link to the task appears on the task, unread for the owner of the key the agent used (see [Notes](help:notes)). Answer in a chat: the agent writes your answer into the task's log, and on your word sends the task back to ready, where any agent can continue it. Returning a task to the initial, ready or ask status clears its `assignee`.

## Drafts

A task on the initial status is a draft: agents do not take it until you say so. Work an agent discovers by itself while inside another task becomes a new draft under the same parent, never part of the current task.

## Reserving a task for an agent

Put `assignee: <agent name>` in the header of a resting task (initial, ask or ready): other agents skip it, and only the named one takes it. An agent's question moves the task to `ask` and clears the assignment, so repeat it after you answer.

## Closing

Ask the agent to close the task, or change `status:` in the header by hand; a claim no longer holds once the status is not the claimed one. A closed top-level task moves to the end of the `archive` document. A task with open subtasks cannot be closed.

To cancel a task, list a `dropped` status among `closed` in the schema; ask the agent to cancel the task, and it closes it as `dropped` with the reason in the log (the `lore` console client has `task drop` for the same).

## Who runs tasks

- The built-in agent, through its `tasks` skill (see [Skills](help:skills)).
- An external agent on an API key, over MCP or with the `lore` console client (see [External agent (MCP)](help:external-agent)).
