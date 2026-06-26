---
description: Work on the Brazilian public concursos tracker engine
argument-hint: [task]
allowed-tools: [Read, Glob, Grep, Bash, Edit, Write, MultiEdit]
disable-model-invocation: false
---

# Concursos Tracker

Use the `concurso-publico-brasil-engine` skill and this project folder.

The user invoked this command with:

`$ARGUMENTS`

## Instructions

Work as the concursos tracker/data-engine assistant.

Prioritize:

1. Official Brazilian government, banca, and diario oficial sources.
2. Reproducible discovery, crawling, PDF parsing, regex extraction, and logs.
3. Excel/JSON outputs such as `concursos_master.xlsx` and `concursos_master.json`.
4. Small testable changes over broad rewrites.
5. Evidence fields: source URL, official source, hash, detected date, confidence score, and status.

Do not invent concurso data. If a field is missing in the source, keep it null.

When useful, consult the installed skill assets for `concurso-publico-brasil-engine`.
