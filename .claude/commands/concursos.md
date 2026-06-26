---
description: Work on the Brazilian public concursos tracker engine
argument-hint: [task]
allowed-tools: [Read, Glob, Grep, Bash, Edit, Write, MultiEdit]
disable-model-invocation: false
---

# Concursos Tracker

The user invoked this command with:

`$ARGUMENTS`

## Instructions

Work as the concursos tracker/data-engine assistant for Rio Grande do Sul (RS).

### Current Phase: Municipality Resource Discovery

The current focus is finding the **stable index/listing page** for concursos and processos seletivos in each RS municipality. This phase does NOT extract individual editals or PDFs — that is the next phase (scanner de indices).

### Pipeline Architecture: 5-Tier Cascade

```
Tier 0: Site oficial (find/confirm prefeitura domain)
Tier 1: Free links (HTML menus, anchors, sitemap, transparencia)
Tier 2: Grounded search (Gemini + Google, only if Tier 1 incomplete)
Tier 3: Gemini verifier/selector (ai_pick_best among valid candidates)
Tier 4: Playwright navigation agent (last resort, menu-directed)
```

### Critical Rules

1. **No numeric scorers.** Do not use point systems (score=85, +150, -220). Use discrete decisions (indice_oficial, revisar, nao_encontrado) and ai_pick_best for choosing between valid candidates.
2. **No hardcoded portal patterns.** Do not add rules for specific providers (multi24, secao=dinamico, IP addresses, specific subareas). Each rule fixes one site and breaks the next.
3. **Precision over coverage.** Zero false positives matters more than finding every municipality. If unsure, mark as `revisar`.
4. **Content over slug.** A page at `/documentos` that lists PSS is valid; a page at `/processos-seletivos` that is empty is not.
5. **Index pages only.** Accept listing/category/portal pages. Reject individual editals, PDFs, news articles, detail pages, licitacoes, concursos culturais.
6. **Golden set validation.** Run `medir_golden_set.py` after any change to verification or selection logic.

### Source Authority Order

1. Banca organizadora.
2. Prefeitura/official organ site.
3. Diario/FAMURS/publication portals.
4. Radar portals (Ache, PCI, QConcursos) only for discovery/audit, never as final evidence.

### AI Usage

- Gemini discovers (Tier 2, grounded) and selects (Tier 3, ai_pick_best).
- Deterministic verification (content signals) is the safety net.
- Use `gemini-2.5-flash`. Cache grounding results per municipality.
- Use real browser User-Agent headers to avoid 406 blocks.

### What NOT to do

- Do not invent concurso data. If a field is missing in the source, keep it null.
- Do not add scoring constants to fix individual cases.
- Do not crawl entire sites with Playwright — navigate menus by text only.
- Do not accept a detail page as the index page.
- Do not optimize for coverage at the expense of precision.
