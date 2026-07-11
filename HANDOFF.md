# Handoff - Estado del Proyecto (2026-07-11)

Este documento refleja la "única verdad" del estado del proyecto Concursos Tracker tras la auditoría técnica realizada hoy.

## Estado actual
Ejecución de chunks 5/6 pausada. El contrato estructural está validado offline y
queda listo únicamente el canario aislado del Paso 6. Se prioriza la **precisión**
sobre la cobertura.

## Auditoría y hallazgos
La auditoría manual sobre los 618 buckets confirmó que, aunque las promociones estaban bien auditadas, el "triage" de los confirmados supervivientes del *cascade* tenía puntos ciegos.

### Familias de FP identificadas
1. **Noticia-con-números-basura:** Artículos de noticias que contienen números que el validador confunde con números de edital (ej: Itaara C).
2. **Menú-por-año:** Páginas cortas (<1600 caracteres) que solo contienen links a años anteriores, no listados reales (ej: Canudos do Vale C).
3. **Atende-overcount:** Plataformas (ej: atende.net) que cuentan números de edital como concursos distintos, generando duplicados/inflados (ej: Estrela C).

## Roadmap corregido
1. **Completar triage:** Auditar la cola de ~30 municipios dudosos restantes contra el corpus congelado.
2. **Identificación/Corrección:** Categorizar familias de FP y delegar los fixes de lógica a Codex.
3. **Validación:** Ejecutar evaluación contra el *golden set* tras los cambios.
4. **Reinicio de Carga:** Retomar chunks 5-6 solo después de tener estas familias tapadas.
5. **Cierre:** Triage final sobre el corpus completo antes de producción.

## Reglas de oro (Inalterables)
- **Calidad:** Precision > Cobertura. 
- **Verificación:** Decisión discreta + IA (ai_pick_best), nunca scorers numéricos.
- **Transparencia:** Todo municipio marcado como `revisar` debe tener una razón documentada. Vacío no es opción.
- **Seguridad:** No usar Playwright por defecto. Solo si los Tiers 0-3 fallan y la página tiene señales claras de renderizado JS.

## Gap: portal_externo_oficial (Ararica)

- **Sintoma:** la corrida congelada encontro y confirmo las dos URLs externas de Ararica mediante `t1+t3` (`data/fase2/municipios_rs_local.csv:20`), pero la cascada no puede emitir de forma demostrable la decision `portal_externo_oficial`: la etiqueta solo esta declarada (`scripts/fase2_municipios/cascade_municipios.py:1155-1165`) y la conversion vigente de `forma`/`tipo` produce `indice_oficial` o `indice_oficial_combinado` (`scripts/fase2_municipios/cascade_municipios.py:1198-1210`).
- **Causa raiz y frontera:** el destino ya fue descubierto; el gap esta en **(d) clasificacion de una candidata presente**. `Candidate` conserva URL, metodo y texto del menu, pero no la URL origen oficial ni la cadena de redirect/click (`scripts/fase2_municipios/cascade_municipios.py:358-369`). Aunque `fetch_page` sigue redirects y guarda la URL final en `Page` (`scripts/fase2_municipios/cascade_municipios.py:219-236`), Tier 1 deja la URL original en `Candidate.url` y solo adjunta el `Page` (`scripts/fase2_municipios/cascade_municipios.py:628-636`). El CSV final tampoco persiste provenance por candidata ni el codigo de decision (`scripts/fase2_municipios/cascade_municipios.py:2284-2290`).
- **Por que no se corrige ahora:** inferir oficialidad por IP, dominio o proveedor violaria la regla generica. Tampoco basta con que Gemini valide el contenido: falta demostrar deterministicamente que el destino fue alcanzado desde el sitio oficial y con intencion de concurso/PSS. Sin esa provenance no se cumplen los tres requisitos conjuntamente: origen oficial trazable, intencion del enlace/menu y pagina indice valida.
- **Criterio de aceptacion para la corrida local de Luis:** conservar por cada candidata la URL origen oficial; texto y rol del enlace/menu/boton; URL inicialmente descubierta; URL final despues de redirects o click; metodo de descubrimiento; y resultado/razon del verificador de contenido. Para ambos buckets de Ararica, el replay debe demostrar origen en el sitio oficial, intencion explicita de concurso/PSS, destino final igual al esperado por el golden (`authority_first/data/golden_set_v1.csv:3`) y contenido de indice valido; solo entonces debe emitirse `portal_externo_oficial`. Si cualquiera de esas evidencias falta, el resultado debe quedar `revisar`.

## Contrato estructural cerrado — listo para canario Paso 6 (2026-07-11)

El gap anterior queda implementado sin inferir autoridad por URL. `Candidate`
conserva `provenance`, `source_kind`, `authority`, `identity`, `page_role`,
`evidence_state`, `accessible`, `bucket`, `decision` y `note`. Un portal externo
solo deriva en `portal_externo_oficial` cuando una cadena
`official_navigation/official_referrer/official_brand` identifica el mismo
municipio; sin ella queda `revisar` aunque el dominio o slug parezcan plausibles.

La estructura es independiente del conteo: filtros, tabla/cards, paginación,
categoría o endpoint inequívocos hacen válido un índice con 0, 1 o múltiples
resultados. Noticia → `nao_encontrado`; menú por año sin listado y antibot
incompleto → `revisar`; detalle individual →
`detalle_individual_rechazado`. `fetchable` es alias operacional de
`accessible`, no elegibilidad. La evidencia Playwright queda `renderizada` y se
reutiliza sin segundo GET.

Las ocho decisiones canónicas son `indice_oficial`,
`indice_oficial_combinado`, `portal_externo_oficial`,
`detalle_individual_rechazado`, `licitacao_rechazada`,
`concurso_cultural_rechazado`, `nao_encontrado` y `revisar`.
`pagina_generica_rechazada` no tenía consumidores: se plegó a
`nao_encontrado/revisar` sin flips atribuibles en los 618 fixtures.

### Canario aislado Paso 6 — no ejecutado

Entrada: `data/fase2/canario_paso6_municipios.txt`. Salida dedicada:
`data/fase2/canario_paso6.csv`. Contiene exactamente Barros Cassal, Boa Vista do
Sul y Progresso. Orion debe correr desde Brasil:

```bash
for municipio in "Barros Cassal" "Boa Vista do Sul" "Progresso"; do
  .venv/bin/python scripts/fase2_municipios/cascade_municipios.py \
    --municipio "$municipio" --append --skip-existing \
    --output data/fase2/canario_paso6.csv
done
```

No usar `data/fase2/municipios_rs_local.csv` y no ejecutar chunks 5/6 durante
este canario.
