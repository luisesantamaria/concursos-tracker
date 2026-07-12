# ROADMAP — Concursos Tracker

El proyecto completo dividido en fases: **de dónde venimos, dónde estamos y a
dónde vamos**. Cada fase tiene objetivo, entregable y gate de salida; el
detalle ejecutable (pasos, comandos, ramas de fallo) vive en `PLAN_MAESTRO.md`
bajo la MISMA numeración. Estado: ✅ hecha · 🔄 en curso · ⬜ pendiente.

```
PASADO                      PRESENTE                    FUTURO
F0 ✅ ─ F1 ✅ ─────────── F2 🔄 ─────────── F3 ⬜ ─ F4 ⬜ ─ F5 ⬜ ─ F6 ⬜ ─ F7 ⬜ ─ F8 ⬜
origen  bancas RS    descubrimiento       industria-  señal    monito- extrac- Brasil  producto
                     municipal RS         lizar       demand-  reo     ción            (app)
                     (motor V2)           descubrim.  driven
```

---

## De dónde venimos

### FASE 0 — Origen y pivote ✅ (2026, primeros meses)
- **Qué fue**: prototipo "Ache-first" (agregador como fuente) → pivote al
  modelo de **fuentes de verdad convergentes**: la autoridad se asigna por
  tipo de hecho; los radares solo descubren. Se definió el contrato de scope
  RS y la tabla base.
- **Entregable**: modelo de autoridad + matriz fuente × evento + scope RS.
- **Lección que gobierna todo lo demás**: precisión > cobertura; el radar
  engaña; la fuente oficial prueba.

### FASE 1 — Bancas RS ✅
- **Qué fue**: crawlers de las bancas que operan en RS (Fundatec, Legalle,
  La Salle, Quadrix, Objetiva, Cebraspe...), con reparación asistida por IA.
- **Entregable**: `scripts/fase1_bancas/` + base de certames de bancas.
- **Rol futuro**: insumo de la señal de actividad (F4) y de la extracción (F6).

## Dónde estamos

### FASE 2 — Descubrimiento municipal RS 🔄 (la fase actual)
Encontrar la **página índice estable** de concursos y PSS de cada municipio
RS. No extrae editais (eso es F6).

- **2a ✅ Cascada de descubrimiento (V1)**: tiers 0-4 (dominio → links →
  búsqueda grounded → selector IA → Playwright) + golden set manual de 24
  municipios + evaluador. Auditorías detectaron las familias de FP (noticias,
  menús vacíos, licitações) → nació la necesidad de V2.
- **2b ✅ Motor V2 de adjudicación (AI-first)**: certificador A con citas
  literales verificadas por código + fiscal adversarial B + juez C + gate
  estructural (autoridad/identidad por evidencia, registro de dominios,
  render-once para SPAs). Independiente de las heurísticas V1 (test
  arquitectónico). **Resultado medido: golden 22/36 con 0 FP; sobre evidencia
  idéntica V2 22/23 vs V1 2/23; 438 tests verdes.** Fixture envenenado:
  **FP=0/15, capturas netas B/C=0**, free=41, paid=0.
- **2c 🔄 Cierre RS** (pasos F2.P1-P8 del PLAN_MAESTRO): fixes mecánicos ✅ →
  política de índice ✅ → fixture envenenado ✅ → R4 golden ✅ (30/36, 0 FP) →
  adjudicación humana ✅ (30/36 ratificado por Luis, 0 FP) → **holdout ciego
  de 50 (F2.P6, paso actual)** → sonda de cuota → corrida de los 497 con
  auditoría muestral.
- **Gate de salida**: mapa RS completo con provenance, FP=0 auditado por
  muestreo, tasa de revisión humana ≤~25%.

## A dónde vamos

### FASE 3 — Descubrimiento industrializado ⬜
Cerrar el hueco que V2 no cubre: PROPONER candidatas a escala sin curación
manual. Activar Tier 1.5 (patrones por plataforma CMS, ~50% de cobertura) como
proponente principal; registro dominio↔código IBGE (Wikidata + sondeo DNS);
agente navegador solo para la cola larga; **exploración interactiva acotada**
(paginación, filtros de año, "consultar") para índices que exigen navegar —
whitelist segura, evidencia por estado con provenance del camino (F3.P5).
- **Gate**: en RS, ≥45% de unidades propuestas por patrón con WRNG=0; registro
  ≥95% de dominios RS automático.

### FASE 4 — Señal de actividad (demand-driven) ⬜
La pieza que convierte "verificar 5.570 municipios" en "verificar los ~800 con
actividad": Querido Diário + bancas + radar detectan DÓNDE hay certame nuevo;
esa señal dispara descubrimiento/verificación solo donde importa (~$15-20/año
de IA y ~12 h-humano/año vs 208 días y ~200 h del backfill exhaustivo).
- **Gate**: recall ≥80% de los certames RS conocidos del período; ≥90% de lo
  señalado termina con fuente confirmada o en cola humana con SLA <7 días.

### FASE 5 — Monitoreo continuo (Plano B) ⬜
Scheduler de relecturas con frecuencia adaptativa + diff de contenido
normalizado + clasificación IA-con-citas del cambio (certame nuevo / documento
nuevo / ruido) + detector de URL rota que re-dispara descubrimiento.
- **Gate**: 30 días detectando los editais nuevos reales de RS con 0 avisos
  falsos y SLA de detección 24-48h.

### FASE 6 — Extracción y consolidación (Plano C) ⬜
De páginas y PDFs a **certames estructurados**: extracción de editais (cargos,
escolaridade, remuneração, cronograma) con citas por página; **resolución de
identidad multi-fuente** (la misma entidad desde banca+prefeitura+diário —
clave órgão+edital+año+banca); timeline de eventos tipificada.
- **Gate**: golden de extracción de 30 editais anotados a mano — ≥90% de
  campos núcleo correctos, 0 campos inventados; 50 certames RS con timeline
  completa auditada.

### FASE 7 — Expansión nacional ⬜
Demand-driven, nunca backfill: goldens chicos (10-20 municipios) en 2-3 UFs
diversas ANTES de habilitar confirmación automática por estado; registro
IBGE nacional; corridas desde Brasil (geo-block); auditoría muestral por lote
hasta certificar 0-FP estadísticamente (n≥300).
- **Gate por UF**: mismas métricas que F2 (FP=0, revisión ≤30%).

### FASE 8 — Producto (portal/app) ⬜
Según `MANUAL_APP.md` (9 etapas): DB compartida + API de catálogo (puede
arrancar al cerrar F2) → web pública → cuentas/LGPD → matching y alertas
(requiere F5) → beta cerrada RS.
- **Gate**: beta con ~50 usuarios reales de RS; alerta real entregada <48h de
  la publicación; retención semana 4 que justifique expandir.

---

## Dependencias entre fases

- F2 es el gate de todo: el patrón golden→holdout que valida el motor se
  repite en cada fase posterior.
- F3 y F4 son paralelas entre sí; ambas alimentan F5.
- F6 puede empezar por bancas (F1) sin esperar F5.
- F8 sprints 1-3 (DB/API/web) solo requieren F2; las alertas requieren F5.
- F7 requiere F3+F4 maduras (el descubrimiento automático es lo que escala).

## Principios permanentes (no cambian de fase)

Verificación por contenido, no por slug · sin scorers numéricos · cero FP con
protocolo STOP · golden manual como oráculo, holdout como prueba · IA adjudica
contenido, código verifica hechos · sin hardcodes por municipio/portal · los
aprendizajes entran solo como hechos curados con provenance humana.
