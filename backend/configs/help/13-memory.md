# Memory

Project memory is a set of facts extracted from the materials. Each fact is a separate document in the Memory folder of the system section. Each fact records which references it was taken from. When a fact is refined, the previous wording is kept in its history, and the agent checks against it during later consolidations.

The agent searches memory when answering questions about the project ("what did we decide about the economy?") and answers with links to the sources.

## Consolidation

The agent turns a document's references into memory facts. Run it by opening the document with the materials and asking: "consolidate memory for this document". By default the document and its whole subtree are processed.

What happens:

1. The agent takes the references one at a time. It reads long materials in parts.
2. For each portion it decides which facts are new, which repeat ones already known, and which refine or contradict them.
3. A refined or contradicted fact is rewritten in place, and the old wording stays in its history. Repeats are merged into the already known fact.
4. The agent moves on to the next portion, and so on to the end.

The process runs in the chat: you can follow it, correct the agent and answer its questions.

## Tips

- Gather one call's or discussion's materials under one document before consolidating: facts are tied to their sources.
- Run consolidation again after new references are added.
- To search memory only, switch the "Search" tab to semantic mode and turn on "Memory only".
- To check a fact, ask the agent to open its sources: every fact links to the references it was taken from.
