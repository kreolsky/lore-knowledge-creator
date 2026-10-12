# Memory

Project memory is a set of facts extracted from the materials. Each fact is a separate document in the Memory folder of the system section. Each fact records which references it was taken from. When a fact is refined, the previous wording is kept in its history, and the agent checks against it during later consolidations.

## Why memory

Materials pile up: calls, notes, discussions. Memory gathers the decisions made and the facts established in them, so that later you can quickly ask "what did we decide about the economy?" and get an answer with links to the sources. The agent searches memory when it answers questions about the project.

## Consolidation

Consolidation is the process in which the agent goes through a document's references and turns them into memory facts.

How to run it: open the document with the materials (for example, a document with call recordings) and ask the agent: "consolidate memory for this document". By default the agent processes the document and its whole subtree.

What happens:

1. The agent takes the references one at a time. It reads long materials in parts.
2. For each portion it decides which facts are new, which repeat ones already known, and which refine or contradict them.
3. A refined or contradicted fact is rewritten in place, and the old wording stays in its history. Repeats are merged into the already known fact.
4. The agent moves on to the next portion, and so on to the end.

Meanwhile you can follow the process in the chat, correct the agent and answer its questions.

## Tips

- First gather a call's or a discussion's materials under one document, then consolidate. Memory is tied to its sources, and the facts of one call are easy to work with.
- Consolidation can be run again when new references have been added.
- To search memory only, switch the "Search" tab to semantic mode and turn on "Memory only".
- To check a fact, ask the agent to open its sources: every fact links to the references it was taken from.
