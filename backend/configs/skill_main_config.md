---
name: main-config
description: Your own configuration — save a new skill or update an existing one from what this chat learned (save_skill), edit a Rule, and when to load a skill or the memory skill. Use when the user asks to keep a procedure as a skill, to change a rule, or to remember something; «сделай скилл», «добавь правило», «запомни», «улучши скилл».
tools:
  - save_skill
---

YOUR OWN CONFIGURATION
- The Rules in your prompt and your skills are documents in this project; the user reads and
  edits them too. Edit a Rule the way you edit any document; load a skill with the
  `skill` tool when the task matches its description, and fix it when its
  instructions turn out to be wrong.
- When the user asks you to remember something or to consolidate memory, load the
  `memory` skill.
