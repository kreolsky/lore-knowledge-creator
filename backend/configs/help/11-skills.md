# Skills

A skill is a saved procedure for the agent: knowledge, a sequence of steps, sometimes a set of tools. The agent loads a skill when the task matches its description, or when asked by name. If you explained the same procedure to the agent more than once, make it a skill.

## Creating a skill

After a task went well, ask the agent: "save this as a skill". The skill is saved as an ordinary document in the Skills folder of "Agent System"; edit it by hand or ask the agent to fix it. Large skills can be split across child documents.

## Skills worth setting up

- **Processing notes and calls** — extract everything from the materials (several passes if needed), structure it, flag contradictions with where each version was said, and show how opinions changed by date.
- **Document review** — a critical pass over the document, then support and contradictions in the references and neighbouring documents; gaps, inconsistencies, open questions.
- **Intent document** — from processed materials, a minimal document: the intent and the decisions on borderline cases, without what context already gives.

## Built-in skills

| Skill | What it does |
|---|---|
| web-search | Web search and reading pages, including a pasted link. Any research request that does not name deep research. |
| deep-research | Runs only when deep research is asked for by name. Proposes a research plan for approval, then works for many minutes; findings become references of the open document, the report a child document. |
| image-generation | Generates and edits pictures; results are saved as references. |
| sandbox | A Linux console: running code, calculations over data, reading raw files. |
| memory-consolidation | Turns a document's references into [memory](help:memory) facts. |
| chat-recall | Finds what you said in your other chats of this project. Never sees other people's chats or chats marked "Unindexed". |
| extractor-config | Writes and edits [extractor](help:pipelines) configs. |
| digest-config | Writes [digest](help:pipelines) configs and explains why a digest is empty. |
| skill-authoring | Saves a finished procedure as a skill, or improves one. |

A project skill with the same name replaces the built-in one. The administrator can switch a built-in skill off.
