# Instructions for Codex / non-Claude agents

**Single source of truth: read `CLAUDE.md` first.** All operating rules live
there (untouchable files, phase definitions, no-numeric-scorers, zero-FP
policy, commands). This file is a pointer, not a second rulebook — if
anything here ever contradicts `CLAUDE.md`, `CLAUDE.md` wins.

## Orientation (read in this order)

1. `CLAUDE.md` — operating rules and constraints.
2. `PLAN_MAESTRO.md` — the executable plan of record (phases F2-F8; each step
   has prerequisite, actions, success proof and failure branch). Find the
   first step not marked `✅` — that is where the project is.
3. `ROADMAP.md` — the full phase map (where we came from / are / are going).
4. `MANUAL_IMPLEMENTACION.md` / `MANUAL_APP.md` — engine architecture and app.

## Living docs (mandatory, same rule as CLAUDE.md)

A session that closes a plan step, runs an evaluation or changes project
state is **not finished** until it updates the living docs — `README.md`
(Status table + phase badge), `ROADMAP.md` (phase states) and
`PLAN_MAESTRO.md` §0 (+ mark the step `✅ (date)`). Checklist and consistency
rules: `.claude/skills/actualizar-docs/SKILL.md`. Hard rules: only facts
backed by an artifact; never mark a step done without its PRUEBA fulfilled;
the same numbers in all three docs; commit docs together with the work that
produced them.

## Hard constraints (summary — full list in CLAUDE.md)

- Zero false positives > coverage; honest `revisar` beats a guess; one FP
  found = STOP protocol.
- No numeric scorers; no hardcoded per-municipality/portal rules.
- Untouchable files (do not edit): `scripts/eval/verdict_extract.py`,
  `scripts/fase2_municipios/cascade_municipios.py`, `data/golden_set_v1.csv`,
  frozen runs under `staging/`.
- Never commit secrets, `.env`, generated outputs or `staging/` artifacts.
- Evaluation runs: free tier only (`paid_calls=0`), frozen code during runs,
  new output dir + new seed per run.
