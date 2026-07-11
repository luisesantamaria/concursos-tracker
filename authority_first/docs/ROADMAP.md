# Roadmap - Authority First RS

## Fase A - Esqueleto y contrato de scope ✅

- Crear estructura V2.
- Declarar matriz fuente x evento.
- Crear filtro duro RS.
- Copiar V1 Ache-first a laboratorio.

## Fase A2 - Tabla base MVP ✅

Antes del timeline completo de eventos, crear `concursos_base_rs`.

Columnas esenciales: semaforo, tipo, orgao, municipio, numero, banca, pagina_oficial, edital_abertura_url.

## Fase B - Crawlers de bancas RS ✅

Legalle, La Salle, Fundatec, Quadrix, Objetiva, Cebraspe, Selecao/Fenix.

## Fase C - Descubrimiento de recursos municipales 🔄

Objetivo: descubrir la **pagina indice/listado estable** de concursos y processos seletivos de cada municipio RS.

**IMPORTANTE:** Esta fase NO extrae editais individuales. La salida es la URL de categoria/indice donde la prefeitura lista todos los concursos o PSS.

### Arquitectura: Cascata de 5 Tiers

```
Tier 0: Site oficial (dominio base)
Tier 1: Links gratuitos (menus HTML, anchors, transparencia)
Tier 2: Grounded search (Gemini + Google, solo si falta)
Tier 3: Gemini selector (ai_pick_best devuelve candidate_id entre records elegibles)
Tier 4: Agente de navegacion Playwright (ultimo recurso, dirigido)
```

### Reglas clave

- Verificacion por CONTENIDO, no por slug de URL.
- Sin scorer numerico: decisiones discretas + ai_pick_best.
- Precision sobre cobertura: zero falsos positivos.
- ~20% requiere revision humana — aceptable.
- No hardcodear patrones de un proveedor de portal.

### Medicion

- Golden set: 24 municipios verificados a mano.
- Script `medir_golden_set.py` mide precision y cobertura por tipo.
- Ejecutar despues de CUALQUIER cambio al verificador o selector.

### Estado actual - 2026-07-11

La corrida amplia sigue pausada. El contrato estructural y los 618 fixtures ya
pasaron validacion offline; el siguiente paso es solamente el canario aislado de
Barros Cassal, Boa Vista do Sul y Progresso. Chunks 5-6 no se ejecutan aun.

Familias de FP detectadas: noticias individuales clasificadas como indice, paginas genericas de menu sin listado real, y sobre-conteo por resultados duplicados/inflados. Primero mapear la cola, corregir cada familia en el pipeline y validar contra golden; solo despues retomar fase 2 chunks 5-6.

### Pendencias

- [x] Implementar ai_pick_best sin scorer.
- [x] Headers de navegador real (fix 406).
- [ ] Cache de grounding por municipio.
- [x] Deteccion de JS + fallback Playwright dirigido con snapshot reutilizable.
- [ ] Escalar golden set con municipios de otras letras.
- [x] Auditar la cola run497 y cerrar noticia/menu/detalle; el conteo inflado ya no decide aceptacion.

## Fase D - Scanner de indices (siguiente)

Objetivo: entrar a cada pagina indice descubierta en Fase C y extrair editais/eventos individuales con scraping.

Salida por evento: titulo, tipo_evento, url_documento, url_pdf, edital_num, data, hash, first_seen.

Esta fase construye el dataset de concursos. Fase C dio la puerta de entrada; Fase D entra y cataloga el contenido.

## Fase E - Diario municipal/FAMURS

Objetivo: cobrir homologacoes, convocacoes, nomeacoes y eventos administrativos.

- Adapter de Diario Municipal FAMURS para municipios sin ruta clara.
- Adapters dedicados para ~15 municipios grandes con DOM propio.

## Fase F - Normalizacion

Resolver identidad de concurso, tipo, edital_num, orgao, municipio, banca, estado del ciclo.

## Fase G - Auditor Ache

Ache deja de ser fuente principal y se convierte en comparador: concursos que faltan en el master, falsos positivos, recall por banca/municipio.

## Contrato estructural Fase 2 — 2026-07-11

La unidad de verdad es una superficie oficial, estable y reutilizable; Fase 2 no
extrae eventos ni PDFs. El contrato ejecutable vive en
`scripts/eval/verdict_extract.py` y separa dimensiones que antes estaban
mezcladas:

- `source_kind`: `dominio_oficial_prefeitura | portal_externo_delegado | banca | diario | desconocido`.
- `authority` e `identity`: triestado `confirmada | rechazada | desconocida`;
  ausencia de evidencia no equivale a rechazo y la autoridad nunca se infiere
  por slug.
- `page_role`: `indice_listado | indice_combinado | detalle_individual |
  noticia | menu_sin_listado | incompleto_antibot | desconocido`.
- `evidence_state`: `completa | incompleta_antibot | renderizada | error_fetch`.
  `accessible=False` solo corresponde a `error_fetch`.
- `bucket`: `concurso_publico | processo_seletivo | combinado`, decidido por
  contenido.
- `decision`: vocabulario canónico cerrado:
  `indice_oficial | indice_oficial_combinado | portal_externo_oficial |
  detalle_individual_rechazado | licitacao_rechazada |
  concurso_cultural_rechazado | nao_encontrado | revisar`.

Mapeo discreto: un índice con autoridad e identidad confirmadas se acepta; el
portal externo requiere cadena de navegación oficial explícita; noticia deriva
en `nao_encontrado`; menú sin listado y antibot incompleto derivan en
`revisar`; detalle, licitación y cultural conservan sus rechazos. Un índice es
válido con **0, 1 o múltiples resultados** si filtros, tabla/cards, paginación,
categoría o endpoint prueban inequívocamente la estructura. `certame_unico` no
rechaza por sí solo.

`Candidate.fetchable` queda como alias operacional de `accessible`; la
elegibilidad vive en `page_role/decision`. Una página accesible pero rechazada
permanece con `decision+note`. Toda candidata de Tier 1, grounded, directed o
Playwright atraviesa la misma hidratación y conserva un `EvidenceSnapshot`
estático o renderizado.

La cadena única en memoria es `CandidateRecord -> SelectedResource ->
FinalDecision`. Record y snapshot son profundamente inmutables; autoridad,
identidad, rol, evidencia, bucket, decisión y razón se calculan una vez antes de
Tier 3. Tier 3 solo devuelve un `candidate_id` existente/elegible. La selección
retiene la instancia exacta y el cierre deriva el estado sin refetch ni segunda
adjudicación; legacy URL-only captura una evidencia y usa el mismo adjudicador,
siempre con razón.

`candidate_id` v1 es `v1:` + SHA-1 de URL final normalizada (host minúsculo sin
`www`, sin fragmento/slash final, query ordenada), source, tier, municipio,
bucket y huella del snapshot. No reconstruye records. Redirect/canonical guarda
requested/final y valida URL/contenido final. El CSV conserva su esquema:
provenance mínima y razón se registran en `razao`/`notes`. Telemetría JSON estable
por candidato/bucket usa `fase2.cascade` en stderr (`FASE2_LOG_LEVEL`).

`pagina_generica_rechazada` era solo una constante Tier 3 sin consumidores ni
veredictos en el corpus. Se plegó a `nao_encontrado/revisar` según estructura;
el replay de 618 fixtures no mostró ningún flip atribuible a ese nombre.

Estado: cadena única y canario Barros Cassal verdes offline, matriz contractual
10/10, suite completa verde y replay run497 sin flips frente a 2b0dc11. Los
chunks reales 5/6 siguen fuera de alcance de este cambio.
