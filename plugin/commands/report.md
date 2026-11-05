---
description: What lintex caught in this session, what the agent fixed, and what is still open
argument-hint: [--since 7d | --all]
allowed-tools: Bash(lintex report *)
---

Run `lintex report $ARGUMENTS` in the project root and show the output.

Then, in two or three sentences, say what it means: how many findings lintex caught, how many
were fixed on a later edit, and which are still open with their file and line. If anything is
still open, offer to fix it now. If the ledger is empty, say that the hook fills it as edits
are checked and that `lintex doctor` confirms the hook is installed.
