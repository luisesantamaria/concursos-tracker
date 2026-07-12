# Roadmap — Concursos Tracker

> **⚠ Este documento es HISTORIAL + mapa de fases.** El plan ejecutable con
> pasos, gates y ramas de fallo vive en **`PLAN_MAESTRO.md` (raíz del repo)**
> — ese documento manda. La visión de producto y la arquitectura de 4 planos
> están en `MANUAL_IMPLEMENTACION.md`; la construcción de la app en
> `MANUAL_APP.md`. Reglas operativas e intocables: `CLAUDE.md`.

## Estado — 2026-07-12

- **Fase C (descubrimiento municipal RS) casi cerrada con el motor V2**:
  golden 36 → 22 match / 0 falsos positivos (R3,
  `staging/fase2_v2/eval/golden36_fable_20260712_r3/`); en evidencia idéntica
  V2 acierta 22/23 vs 2/23 de las heurísticas V1. Quedan los pasos F1.P1-P8
  del PLAN_MAESTRO (fixes mecánicos, fixture envenenado, R4, holdout 50,
  corrida 497).
- Piezas nuevas del 12-jul: independencia total V1/V2 (test arquitectónico
  AST+runtime), registro de dominios oficiales con provenance humana,
  autoridad por cadena de redirect, render-once para SPAs (atende/elotech),
  modo `--no-v1-differential`, `semantic_comparison.py`.
- Análisis de escala (síntesis adversarial): la estrategia nacional es
  **demand-driven** (señal de actividad → verificar solo municipios activos),
  no backfill exhaustivo. Números y ramas en PLAN_MAESTRO §0 y FASE 3.

## Mapa de fases (histórico → plan actual)

| Fase histórica | Estado | Continúa como (PLAN_MAESTRO) |
|---|---|---|
| A/A2 — Esqueleto + tabla base | ✅ | — |
| B — Crawlers de bancas RS | ✅ | insumo de FASE 3 (señal) y FASE 5 (extracción) |
| C — Descubrimiento municipal RS | 🔄 | **FASE 1** (cierre RS) + **FASE 2** (industrializar descubrimiento: Tier 1.5 a escala, registro IBGE, goldens multi-UF) |
| D — Scanner de índices (editais/eventos) | pendiente | **FASE 4** (monitoreo) + **FASE 5** (extracción) |
| E — Diário/FAMURS | pendiente | **FASE 3** (señal Querido Diário) + FASE 5 (eventos administrativos) |
| F — Normalización / identidad | pendiente | **FASE 5** (resolvedor de identidad multi-fuente) |
| G — Auditor Ache | pendiente | transversal: radar = descubrimiento/recall-check, nunca autoridad |
| — (nuevo) Producto/app | pendiente | **FASE 6** → `MANUAL_APP.md` |

## Reglas clave vigentes (sin cambios)

- Verificación por CONTENIDO, no por slug. Sin scorer numérico. Precisión
  sobre cobertura: cero falsos positivos. Sin hardcodes por municipio/portal.
- Golden set manual = oracle de desarrollo; holdout = prueba de
  generalización. Correr el evaluador tras CUALQUIER cambio de
  verificación/selección.
- Nuevo desde 12-jul: la adjudicación semántica es EXCLUSIVA de los agentes
  IA (A/B/C) con citas literales verificadas por código; el código solo
  verifica hechos objetivos (autoridad/identidad/accesibilidad). V1
  (`verdict_extract`) queda como baseline comparativo ex-post, nunca en el
  camino decisor.

## Referencia: contrato estructural Fase 2 (2026-07-11)

La unidad de verdad es una superficie oficial, estable y reutilizable; Fase 2
no extrae eventos ni PDFs. El contrato ejecutable vive en
`scripts/eval/verdict_extract.py` (hoy: baseline V1, protegido) y separa:

- `source_kind`: `dominio_oficial_prefeitura | portal_externo_delegado | banca | diario | desconocido`.
- `authority` / `identity`: triestado `confirmada | rechazada | desconocida`;
  la ausencia de evidencia no es rechazo y la autoridad nunca se infiere por slug.
- `page_role`, `evidence_state` (`completa | incompleta_antibot | renderizada | error_fetch`),
  `bucket` (`concurso_publico | processo_seletivo | combinado`).
- `decision` (vocabulario cerrado): `indice_oficial | indice_oficial_combinado |
  portal_externo_oficial | detalle_individual_rechazado | licitacao_rechazada |
  concurso_cultural_rechazado | nao_encontrado | revisar`.

Un índice es válido con 0, 1 o múltiples resultados si filtros, tabla/cards,
paginación o categoría prueban la estructura. `candidate_id` = `v1:` + SHA-1
de URL normalizada + source + tier + municipio + bucket + huella de snapshot.
En V2 estos mismos hechos objetivos los computa
`scripts/fase2_municipios/v2/eval/structural_evidence.py` (espejo estructural
sin el clasificador semántico), y la decisión la producen los agentes A/B/C
bajo el gate de `scripts/fase2_municipios/v2/agents/orchestration.py`.
