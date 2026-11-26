# Skills

A skill is a saved procedure for the agent: knowledge, a sequence of steps, sometimes a set of tools. The agent loads a skill when the task matches its description, or when asked by name. If you explained the same procedure to the agent more than once, make it a skill.

## Creating a skill

After a task went well, ask the agent: "save this as a skill". The skill is saved as an ordinary document in the Skills folder of "Agent System"; edit it by hand or ask the agent to fix it. Large skills can be split across child documents.

## Skills worth setting up

- **Processing notes and calls** — extract everything from the materials (several passes if needed), structure it, flag contradictions with where each version was said, and show how opinions changed by date.
- **Document review** — a critical pass over the document, then support and contradictions in the references and neighbouring documents; gaps, inconsistencies, open questions.
- **Intent document** — from processed materials, a minimal document: the intent and the decisions on borderline cases, without what context already gives.

## Built-in skills

The `main-` skills are the agent's basic set: everything it does with documents. They are loaded from the first message in every chat, with or without a persona, and a persona decides which of them it starts with (see [Personas and rules](help:personas-rules-knowledge)).

| Skill | What it does |
|---|---|
| main-read | Opens documents, shows the tree around the open document, searches the project, and writes links to documents, references and web pages. |
| main-edit | Creates, edits, appends to and renames documents; how a change lands, and how embeds, highlights and tables are written. |
| main-files | Saves a file as a reference and looks at an image. |
| main-config | Saves and updates skills, and edits the agent's rules. |
| web-search | Web search and reading pages, including a pasted link. Any research request that does not name deep research. |
| deep-research | Runs only when deep research is asked for by name. Proposes a research plan for approval, then works for many minutes; findings become references of the open document, the report a child document. |
| image-generation | Generates and edits pictures; results are saved as references. |
| sandbox | A Linux console: running code, calculations over data, reading raw files. |
| memory | Everything that writes [memory](help:memory): saves facts from the current chat ("remember this"), turns a document's references into facts, attaches a fact to a document. Switching it off stops every memory tool, reading facts included. |
| tasks | Works the project's [task](help:tasks) documents: finds tasks by the properties in their header, takes the next one, reports back and hands it to review; a question it cannot resolve parks the task and arrives as a note on it. An external coding agent on a key can run the whole loop. Switching it off stops every task tool, listing included. |
| chat-recall | Finds what you said in your other chats of this project, lists them and reads one whole. Never sees other people's chats or chats marked "Unindexed". Switching it off does not stop saving facts from a chat. |
| history | Answers what changed in a document, compares versions, brings back a passage or restores a whole version ([history](help:history)). Switching it off takes version tools away from the agent. |
| tables | Changes existing [tables](help:tables) — cells, rows, columns — and creates a named table or one placed in a specific section. Switching it off leaves the agent able to write a new table into text, but not to edit a table. |
| structure | Moves and reorders documents, turns a document into a reference and back, moves a chat under another document, shows what links to a document, and re-runs the import of a reference whose text is wrong. |
| md-folder | Exports a document tree as a zip of Markdown files for Obsidian or GitHub, and imports a markdown folder by link (for example, a GitHub repository's docs). |
| extractor-config | Writes and edits [extractor](help:pipelines) configs. |
| digest-config | Writes [digest](help:pipelines) configs and explains why a digest is empty. |
| pipelines | Binds an extractor or a digest to a document, runs it, sets or pauses a digest's schedule, removes a binding, and reads the run log to say why a [pipeline](help:pipelines) did or did not run. Binding, running and removing are confirmed like the agent's edits. |
| skill-authoring | Saves a finished procedure as a skill, or improves one. |
| secrets | Calls an outside API with one of your [personal keys](help:profile) — "check the billing with my key". The agent never sees the key's value. |

A project skill with the same name replaces the built-in one. The administrator can switch a built-in skill off, and can upload a skill for the whole server as a zip (`SKILL.md` with its scripts and reference files): every user's agent can then use it, and the skill's card lists its files.
