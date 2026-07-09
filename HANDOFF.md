# Handoff - Estado del Proyecto (2026-07-08)

Este documento refleja la "única verdad" del estado del proyecto Concursos Tracker tras la auditoría técnica realizada hoy.

## Estado actual
Ejecución de nuevos chunks (run497) pausada. Se prioriza la **precisión** (eliminar falsos positivos) sobre la cobertura.

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
