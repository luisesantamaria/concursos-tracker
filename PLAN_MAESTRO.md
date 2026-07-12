# PLAN MAESTRO — Concursos Tracker

**Versión**: 2.0 (12-jul-2026) · **Autoridad**: este es el plan de registro.
`ROADMAP.md` divide el proyecto en las MISMAS fases (F0-F8) con la vista de
alto nivel; `CLAUDE.md` tiene las reglas operativas; los manuales tienen la
arquitectura (`MANUAL_IMPLEMENTACION.md`) y el producto (`MANUAL_APP.md`).

**Cómo usar este documento (para cualquier agente/IA/humano)**: cada PASO
tiene `ENTRADA` (prerrequisito verificable), `ACCIONES` (qué hacer, con
comandos/archivos exactos), `PRUEBA` (criterio de éxito medible) y `SI FALLA`
(la rama a tomar). Ejecutar en orden; no saltar gates. F0 y F1 están hechas
(ver ROADMAP); este plan arranca en la fase actual (F2) y llega al producto
(F8).

---

## 0. Estado verificado al 12-jul-2026 (punto de partida)

- Motor V2 (adjudicación IA): `scripts/fase2_municipios/v2/` — certificador A
  + fiscal B + juez C sobre Gemini flash-lite free, citas literales
  verificadas por código, gate estructural, registro de dominios, render-once
  SPA. Suite: **438 tests verdes** (`pytest scripts/fase2_municipios/v2`).
- F2.P1 cerrado: snapshot directo 400K, fetch 60s con un reintento transitorio,
  reintento integral de unidad ante validación inválida y ausencia legítima
  preservada como `nao_encontrado`/`negative`.
- F2.P2 cerrado: política de índice en skills (contenedor mixto SÍ, feed-tag
  SÍ — decisión de Luis 12-jul; sección sin ítems citables del bucket nunca
  afirmativa) + espejo en fiscal B. sha256 certifier=b2f3fda0…, prosecutor=
  435e6bff….
- F2.P3 cerrado: fixture envenenado v2 de 15 unidades/7 tipos obligatorios;
  R2 en `staging/fase2_v2/eval/fixture_envenenado_20260712_r2/` y reporte
  ex-post `fixture_envenenado_report_20260712_r2/poison_report.json` →
  **FP=0/15, capturas netas B/C=0**, 41 llamadas free, paid=0, 326.796
  tokens. Política resultante: slim por defecto; B/C quedan intactos para
  desacuerdo/muestreo 10%.
- F2.P4 cerrado (decisión de Luis 12-jul, ejecutada por Fable): **R4 aceptado
  como gate — 30/36, 0 FP** (`golden36_fable_20260712_r4/`, 73 free, paid=0).
  **POLÍTICA DE VARIANZA registrada**: el gate se evalúa sobre la corrida de
  config congelada que lo cumplió; la varianza inter-corrida se reporta como
  métrica separada — R5 (`_r5/`, único fix feed/tag, 80 free) dio 26/36 con
  0 FP y sus 5 pérdidas son TODAS varianza de validación del modelo free
  (Proposal/ModelResponseValidationError; ninguna causada por el fix;
  Itaara/PS regresó en R5). **Unión R4∪R5 = 31/36 confirmables demostradas,
  0 FP en todas las corridas históricas.** En producción la convergencia a
  la unión la dan los reintentos del monitoreo (F5); la varianza es el
  argumento empírico para pagado (F2.P7). Canoas/PSS = bug abierto NO
  bloqueante (A sigue rechazando el feed pese al fix; investigar post-P5).
  Matrices `semantic_matrix_r4/r5_20260712/`; informe y tablas P5 en
  `f2_p4_report_20260712/informe_f2_p4_y_entregable_p5.md`. Higiene: seed
  `2026071206` se reutilizó en R4 (inofensivo; futuras corridas seed nuevo).
  Paso exacto siguiente: **F2.P5** (adjudicación de Luis, tablas listas).
- Comparación controlada (`semantic_matrix_r3_20260712/`): sobre evidencia
  idéntica, V2 acierta 22/23 vs 2/23 de las heurísticas V1.
- Fixture y oracle: `url_map_golden_fixture_20260712.csv` (36 URLs verificadas),
  `golden_oracle_manifest_20260712.json`, diff razonado en
  `url_fixture_diff_20260712.csv` (todo en `staging/fase2_v2/eval/`).
- Advertencia estadística vigente: 0-FP demostrado solo sobre n=22
  confirmaciones (cota superior 13.6% al 95%). No prometer 0-FP a escala
  hasta cumplir F2.P8.
- Análisis de escala (síntesis adversarial 12-jul): demand-driven >> backfill
  (~$15-20/año y ~12 h-humano/año vs 208 días free / ~200 h); techo free
  EMPÍRICO ~125 req/día (contradice blogs de 1.500) — verificar antes de
  planear; Tier 1.5 nunca corrido a escala; fiscal B/juez C sin capturas
  netas en 133 unidades pero nunca enfrentaron veneno.

**Comandos base (Windows→WSL; el .venv es Linux):**
```bash
# suite
wsl.exe -e bash -lc 'cd "/mnt/c/.../concursos-tracker" && .venv/bin/python -m pytest scripts/fase2_municipios/v2 -q'
# corrida golden V2 (patrón R3)
wsl.exe -e bash -lc '... .venv/bin/python -m scripts.fase2_municipios.v2.eval.run_golden_live \
  --provider gemini_free --tools none --grounding off \
  --golden data/golden_set_v1.csv \
  --url-map staging/fase2_v2/eval/url_map_golden_fixture_20260712.csv \
  --no-v1-differential --render-fallback \
  --output-dir staging/fase2_v2/eval/<RUN_NUEVO> \
  --credentials-file <env-file-con-GEMINI_API_KEY_FREE> --seed <YYYYMMDDNN>'
# matriz semántica ex-post (sin modelo)
wsl.exe ... -m scripts.fase2_municipios.v2.eval.semantic_comparison --run-dir <RUN> --golden data/golden_set_v1.csv --output-dir <NUEVO>
```

## Reglas transversales (aplican a TODOS los pasos de TODAS las fases)

- **R-T1 · Protocolo STOP por FP**: si una auditoría encuentra UNA afirmación
  publicable equivocada (URL confirmada errada; más adelante: campo extraído
  errado, evento mal tipificado) → parar la fase, causa raíz, corrección
  GENERAL (nunca hardcode por municipio/portal), añadir el caso al fixture
  envenenado de esa fase, re-correr el gate. Ningún avance con FP abierto.
- **R-T2 · No enseñar etiquetas**: los goldens/manifiestos jamás se leen en
  runtime; las decisiones se ganan adjudicando contenido vivo. Aprendizajes
  entran SOLO como hechos curados con provenance humana (registro de
  dominios, promoción v2/memory).
- **R-T3 · Corridas congeladas**: sin cambios de código/prompts durante una
  corrida de evaluación; output a directorio nuevo en `staging/`; seed nuevo;
  corridas previas intactas; sin promoción a datos canónicos desde evaluación.
- **R-T4 · RED→GREEN**: todo fix con test que falla primero; suite verde
  antes de cualquier corrida.
- **R-T5 · Paid=0 y grounding=off** en evaluación salvo autorización
  explícita. Producción usará API pagada (decisión F2.P7).
- **R-T6 · Intocables**: ver CLAUDE.md (verdict_extract.py,
  cascade_municipios.py, golden CSV, corridas congeladas).
- **R-T7 · Cierre de paso**: al terminar cada paso, (a) actualizar los docs
  vivos — README (Status/badge), ROADMAP (estados) y este §0 + marcar el paso
  `✅ (fecha)` — con la skill `/actualizar-docs` (reglas de consistencia en
  `.claude/skills/actualizar-docs/SKILL.md`); (b) registrar en la memoria del
  proyecto qué se hizo, números y el paso siguiente. Un paso sin docs
  actualizados NO está cerrado.
- **R-T8 · Patrón golden→holdout**: toda capacidad nueva (descubrimiento,
  señal, monitoreo, extracción) se valida contra una verdad manual chica y
  luego contra un holdout ciego antes de operarse a escala.

---

# FASE 2 — Cerrar descubrimiento municipal RS (fase actual)

### F2.P1 ✅ (12-jul-2026) — Fixes mecánicos de evidencia/fetch
**ENTRADA**: suite 419 verde.
**ACCIONES** (cada una con test RED→GREEN):
1. `MAX_DIRECT_SNAPSHOT_CHARS` 200.000 → 400.000 en
   `scripts/fase2_municipios/v2/agents/base.py` (Itaqui pesa 223K; flash-lite
   soporta 1M tokens de contexto).
2. Timeout de fetch 30s → 60s + UN reintento ante `TimeoutError`/error de red
   transitorio en `OrionHTTPFetcher`/adapter (NH/PS y Pelotas/CP en R3).
3. UN reintento de unidad (attempt-002, artefacto separado) cuando el fallo es
   `ModelResponseValidationError`/`ProposalValidationError` (varianza del
   modelo free: NH/CP confirmó en R1/R2 y falló en R3 por sampling). El
   reintento pasa por TODO el pipeline (A→B→gate); nunca relaja validación.
4. `nao_encontrado` de A → decisión final `negative` (hoy colapsa a revisar):
   permite que Aceguá/CP puntúe match con golden `no_existe`. Tocar
   orchestration + adapter + scoring del runner; el gate sigue sin publicar
   nada afirmativo sin citas.
**PRUEBA**: suite completa verde; tests nuevos cubren los 4 casos.
**SI FALLA** (algún fix rompe otra cosa): revertir SOLO ese fix, documentar,
continuar con los demás — ninguno depende de otro.

### F2.P2 ✅ (12-jul-2026) — Política de índice (2 reglas generales en skills)
**ENTRADA**: decisión de Luis registrada (12-jul: contenedor mixto = SÍ;
feed-tag = **SÍ, confirmado por Luis**).
**ACCIONES**: en `skills/fase2-resource-certifier/SKILL.md` añadir (general,
sin municipios): (a) contenedor oficial mixto cuenta como índice del bucket
SI contiene ítems citables del bucket; (b) feed/tag oficial agregador cuenta
como listado (una noticia individual sigue rechazada); (c) sección vacía
NUNCA se confirma. Espejo en fiscal B (motivo de acusación si el contenedor
no tiene ítems del bucket).
**PRUEBA**: suite verde; sha256 de skills re-registrados.
**SI Luis dice NO a feed-tag**: solo (a) y (c); Canoas/PS queda como revisar
legítimo y el techo de F2.P4 baja en 1.

### F2.P3 ✅ (12-jul-2026) — Fixture envenenado (mide al fiscal B y blinda el 0-FP)
**ENTRADA**: F2.P1 hecho.
**ACCIONES**: construir `staging/fase2_v2/eval/fixture_envenenado_v1.csv` con
15-20 unidades donde la URL es PLAUSIBLE pero INCORRECTA, de páginas reales
de RS fuera del golden: (tipos obligatorios) licitações, noticia individual
de concurso, índice del bucket contrario, índice de OTRO municipio, detalle
de un edital, sección cultural (soberanas), soft-404 con 200. Correr V2 con
golden sintético (expectativa `rechazo/revisar` por unidad).
**PRUEBA (doble)**: (1) **FP=0**: ninguna envenenada termina `confirmado` —
si una pasa: R-T1 y el hueco es del gate/skills. (2) Conteo de capturas
netas de B/C (venenos que A no detuvo solo).
**SI B/C capturan ≥1 neto**: se quedan. **SI capturan 0**: modo slim por
defecto (A+gate; B/C solo en desacuerdo o muestreo 10%), código intacto.

### F2.P4 ✅ (12-jul-2026) — R4 contra el golden 36
**RESULTADO**: gate cumplido con R4 (30/36, 0 FP); política de varianza y
unión R4∪R5=31/36 registradas en §0; Canoas/PSS bug abierto no bloqueante.
**ENTRADA**: F2.P1-P3 verdes.
**ACCIONES**: corrida R4 (comando patrón §0, output nuevo, seed nuevo);
generar `v2_only_differential` + `semantic_comparison`.
**PRUEBA**: (1) FP=0 — si no, R-T1. (2) match ≥30/36. (3) Cada differ
clasificado: `revisar legítimo` (adjudicado por Luis) o `bug` (→ fix general
→ R5).
**SI match <30 tras R5**: presentar matriz a Luis y decidir si el techo
restante es aceptable (el golden marca 7 unidades requiere_revision_humana).

### F2.P5 — Adjudicación humana de cierre RS-golden
**ENTRADA**: R4/R5 con FP=0.
**ACCIONES**: Luis revisa (a) confirmaciones nuevas, (b) differ marcados
`revisar legítimo`, (c) las 6-7 unidades requiere_revision_humana que V2
confirmó con URL exacta (¿match válido o sobre-confirmación?).
**PRUEBA**: acta escrita (doc en staging) con cada differ adjudicado.
**SI aparece un FP**: R-T1.

### F2.P6 — Holdout 50 (prueba de generalización; NO antes de P5)
**ENTRADA**: acta de P5; autorización explícita de Luis.
**ACCIONES**: 50 municipios de los 497 NO presentes en el golden,
estratificados por plataforma. Descubrimiento SIN curación manual (registro +
Tier 1.5 + cascada) → V2. La verdad se construye DESPUÉS de la corrida (a
ciegas) para las confirmadas + muestra de abstenciones.
**PRUEBA**: FP=0 en confirmadas (duro); precisión ≥95%; revisión ≤30%.
**SI FP>0**: R-T1 + caso al fixture envenenado. **SI revisión >30%**: el
hueco es descubrimiento → adelantar F3.P1-P2 y reintentar.

### F2.P7 — Sonda de cuota + decisión free/pagado
**ENTRADA**: cualquier momento desde F2.P4.
**ACCIONES**: quemar ~200 llamadas en 1 día con 1 key free y registrar dónde
aparece `quota_429` (`approx_rpd` ya se mide).
**PRUEBA**: techo real documentado (¿~125 o ~1.500 RPD?).
**DECISIÓN**: techo <300 RPD → producción con API pagada (~$151 todo Brasil,
trivial); free queda para desarrollo.

### F2.P8 — Corrida 497 RS completa
**ENTRADA**: F2.P6 verde.
**ACCIONES**: correr 497 (menos golden) por lotes de ~50; auditoría muestral
humana de 50-100 confirmaciones POR LOTE con intervalo de confianza.
**PRUEBA**: FP=0 en cada muestra; al acumular n≥300 confirmaciones auditadas
sin FP, la cota superior al 95% baja de ~1% → recién ahí puede DECIRSE
"cero FP" públicamente.
**SALIDA DE FASE**: mapa RS completo con provenance → habilita F5 y F8
(sprints 1-3).

# FASE 3 — Descubrimiento industrializado

### F3.P1 — Tier 1.5 como proponente a escala
**ENTRADA**: F2.P4 (no requiere holdout).
**ACCIONES**: correr las sondas por plataforma (Tier 1.5 en cascade, ya
implementado y golden-limpio; hoy 0/497 filas vienen de él) sobre los 497 de
RS; registrar % que propone candidata y % que coincide con confirmados F2.
**PRUEBA**: WRNG=0 (ninguna propuesta contradice un confirmado auditado);
cobertura de propuesta ≥45% (concentración de plataformas RS ~50%).
**SI WRNG>0**: el patrón de esa plataforma se corrige o se degrada a
"propone-pero-marca-revisar"; jamás parche por municipio.

### F3.P2 — Registro dominio↔IBGE nacionalizable
**ENTRADA**: ninguna (paralelo a F2).
**ACCIONES**: tabla por código IBGE (5.570) con dominio oficial; fuentes en
orden: registro RS existente (`scripts/fase2_municipios/v2/data/`) → Wikidata
P856 (SPARQL/dump) → sondeo DNS de convenciones por UF → verificación de
vida+identidad (script, sin IA). Toda fila con `fuente` y `fecha`.
**PRUEBA**: RS ≥95% automático; muestra manual de 30 filas nacionales sin
error.
**SI Wikidata/DNS cubren <60% nacional**: añadir fuentes (tribunais de
contas, asociaciones estaduales) ANTES de aceptar cola manual masiva.

### F3.P3 — Agente navegador para la cola larga
**ENTRADA**: F3.P1 medido (para conocer el tamaño real del residuo).
**ACCIONES**: agente Playwright + modelo barato que navega el menú del sitio
custom como humano (reutiliza Tier 4 + render V2), propone candidata con la
evidencia de la ruta de navegación (provenance official_navigation). Solo
para municipios sin patrón de plataforma.
**PRUEBA**: sobre 20 municipios custom de RS ya confirmados (a ciegas),
propone la URL correcta ≥70% y NUNCA propone con evidencia inventada.
**SI <70%**: aceptar cola humana para custom; el agente queda como asistente
del revisor (pre-navega y documenta), no como proponente.

### F3.P4 — Embudo integrado
**ENTRADA**: F3.P1-P3.
**ACCIONES**: orquestar registro→patrón→cascada/agente→V2 como pipeline único
con telemetría por etapa (qué % resolvió cada una, costo, tiempo).
**PRUEBA**: sobre una muestra de 100 unidades RS (mezcla de confirmadas y
vírgenes), el embudo end-to-end (sin curación) reproduce ≥85% de las
confirmadas con FP=0.
**SALIDA DE FASE**: descubrimiento sin manos para la mayoría; residuo humano
acotado y medido.

### F3.P5 — Exploración interactiva acotada (paginación/filtros en la evidencia)
**ENTRADA**: F3.P4; o antes, si el holdout F2.P6 muestra ≥5 unidades
bloqueadas solo por interacción.
**ACCIONES**: extender el fallback de render de V2 a una exploración ACOTADA
cuando el índice renderizado no muestra ítems del bucket pero SÍ controles de
exploración: (a) whitelist de interacciones seguras — enlaces de paginación
(hasta 3 páginas), filtro de año → "todos"/ano=0, submit de búsqueda VACÍA,
pestañas vigente/encerrado; JAMÁS formularios con datos, login ni descargas;
(b) cada estado capturado entra como fuente adicional del EvidenceSnapshot
con provenance del camino de interacción (qué control, en qué orden); (c) las
citas se verifican contra el estado que las contiene; (d) límite duro por
unidad (≤5 interacciones, timeout global) y respeto de waf_guard.
**PRUEBA**: golden de interacción chico (10 unidades reales que hoy exigen
paginación/filtro/clic — incluidas las `requiere_revision_humana` que Luis
adjudique automatizables): ≥7/10 llegan a evidencia con ítems del bucket
visibles, 0 FP nuevos, y el fiscal B recibe el camino para fiscalizar.
**SI FALLA** (portales frágiles/raros): esas unidades quedan `revisar` con
nota `requiere_interaccion` y el camino pre-navegado documentado — cola
humana asistida, nunca adivinanza.

# FASE 4 — Señal de actividad (demand-driven)

### F4.P1 — Prototipo Querido Diário sobre RS
**ENTRADA**: ninguna (paralelo a F2/F3).
**ACCIONES**: consultar la API de QD por municipio RS con términos ("concurso
público", "processo seletivo", "edital de abertura") sobre los últimos 60
días; cruzar con certames reales conocidos (bancas F1 + confirmados F2).
Documentar cobertura de QD en RS (qué municipios tienen diário indexado).
**PRUEBA**: recall ≥80% de los certames conocidos del período; precisión de
señal ≥70% (mención real, no ruido).
**SI QD no cubre suficiente RS**: complementar con bancas (F1 ya las
monitorea) + radar Ache/PCI como señal (nunca autoridad); documentar
cobertura por fuente y el residuo ciego.

### F4.P2 — Clasificador de señal
**ENTRADA**: F4.P1.
**ACCIONES**: IA-con-citas (mismo patrón V2, 1 llamada barata) que convierte
una mención del diário en (municipio IBGE, bucket, tipo de evento probable,
cita literal). Golden chico: 50 menciones anotadas a mano.
**PRUEBA**: ≥90% de clasificación correcta sobre el golden de menciones; 0
municipios mal asignados (la cita debe contener el nombre/órgano).
**SI <90%**: iterar prompt/contrato UNA vez; si persiste, degradar a señal
"municipio+fecha" sin tipo (sigue siendo útil para priorizar).

### F4.P3 — Cola demand-driven
**ENTRADA**: F4.P1-P2 + F3.P4.
**ACCIONES**: job que convierte señal→(municipio, bucket) a verificar; si no
hay URL confirmada fresca (TTL 90 días) → dispara embudo F3 + V2; si hay →
dispara lectura de monitoreo (F5). Telemetría: latencia señal→confirmación.
**PRUEBA**: en 30 días, ≥90% de los certames RS señalados terminan con fuente
confirmada o en cola humana con SLA <7 días.
**SALIDA DE FASE**: el sistema decide SOLO qué verificar y cuándo; el costo
pasa de "mapear Brasil" a "~10 llamadas/día".

# FASE 5 — Monitoreo continuo (Plano B)

### F5.P1 — Scheduler con estado por fuente
**ENTRADA**: mapa RS de F2.P8.
**ACCIONES**: tabla `fuente` con frecuencia adaptativa (banca con certame
activo=diaria; municipal con actividad=diaria/semanal; quieto=quincenal;
señal F4 fuerza lectura inmediata). Corridas desde runner Brasil (RUNBOOK).
**PRUEBA**: 7 días operando sobre RS sin saturar dominios (respeta waf_guard)
y sin fuente activa >72h sin check.

### F5.P2 — Detección de cambio (diff)
**ACCIONES**: snapshot normalizado del listado (texto visible del main, no
HTML crudo) + hash; comparar contra el anterior; solo los cambiados pasan a
clasificación. Persistir ambos snapshots (evidencia).
**PRUEBA**: sobre 20 fuentes con cambios sintéticos inyectados (fixture),
detecta 20/20; sobre 50 fuentes reales sin cambios, <5% de falsos cambios
(ruido de fechas/contadores debe normalizarse).
**SI ruido >5%**: mejorar normalización (fechas relativas, contadores,
banners) — general, nunca por sitio.

### F5.P3 — Clasificación del cambio (IA con citas)
**ACCIONES**: para cada diff, 1 llamada barata con el patrón V2 (evidencia
congelada, citas verificadas): ¿certame NUEVO, documento nuevo de certame
conocido, o ruido? Salida = evento tipificado con cita.
**PRUEBA**: golden de 40 diffs anotados (de los cambios reales del período de
F5.P1): ≥90% correcto, 0 eventos inventados.

### F5.P4 — Detector de URL rota/migrada
**ACCIONES**: 404/redirect a home/vacío persistente (2 checks) → marcar
fuente `rota` → re-disparar embudo F3 para ese municipio → V2 confirma la
nueva → la vieja queda en historial con provenance.
**PRUEBA**: simular 5 migraciones (fixture) → 5/5 re-descubiertas o en cola
humana; ninguna fuente rota sigue "confirmada".

### F5.P5 — Gate de fase (30 días en vivo)
**PRUEBA**: 30 días sobre RS: detectar los editais nuevos reales del período
(recall-check contra radar Ache/PCI como auditoría) con **0 avisos falsos** y
SLA de detección 24-48h para fuentes activas.
**SI recall <objetivo**: mapear qué certames se perdieron y POR QUÉ fuente
debió verse (¿hueco de mapa? ¿de señal? ¿de diff?) — el hueco define qué fase
refuerza (F2/F4/F5).

# FASE 6 — Extracción y consolidación (Plano C)

### F6.P1 — Golden de extracción
**ACCIONES**: Luis (o revisión guiada) anota a mano 30 editais reales de
bancas RS (F1): campos núcleo (órgão, nº edital, año, cargos con
escolaridade/vagas/salário, taxa, fechas de inscripción/prueba) + offsets de
la evidencia en el PDF/página.
**PRUEBA**: golden versionado con hash, como el de F2.

### F6.P2 — Extractor de editais (bancas primero)
**ACCIONES**: pipeline PDF→texto (con OCR fallback) → IA con citas por página
(patrón V2: campos + cita literal cada uno; sin evidencia ⇒ null+flag) →
validación de tipos por código (fechas, moneda, enums de escolaridade).
**PRUEBA**: contra el golden F6.P1: ≥90% de campos núcleo correctos; **0
campos inventados** (todo valor sin cita anclada = fallo duro).
**SI OCR/formatos rompen >20%**: cola humana por familia de formato,
documentada; nunca "mejor esfuerzo" silencioso.

### F6.P3 — Resolvedor de identidad multi-fuente
**ACCIONES**: entidad Certame con clave natural (órgão normalizado, nº
edital, año, tipo) + banca; matcher que une menciones de banca (F1),
municipal (F2/F5) y diário (F4) SOLO con evidencia citada de la clave; en
duda, quedan separadas con flag `posible_duplicado`.
**PRUEBA**: sobre 50 certames RS con presencia multi-fuente conocida: ≥95%
unificados correctamente, **0 fusiones incorrectas** (fusionar dos certames
distintos es el FP de esta fase → R-T1).

### F6.P4 — Timeline de eventos
**ACCIONES**: tipificar cada documento/mención (abertura, retificação,
prorrogação, resultado, convocação, homologação, nomeação) y colgarla del
certame con fecha y fuente-que-prueba.
**PRUEBA**: 50 certames RS con timeline completa auditada por Luis; ningún
evento sin documento fuente.
**SALIDA DE FASE**: el dato que el producto necesita: certames estructurados,
completos y con provenance por campo.

# FASE 7 — Expansión nacional (demand-driven)

### F7.P1 — Goldens chicos en 2-3 UFs diversas
**ENTRADA**: F2 cerrada; F3.P4 operando en RS.
**ACCIONES**: golden de 10-20 municipios en 2-3 UFs no-sur (p.ej. BA, PA,
GO); repetir F2.P4-P6 en miniatura por UF (los CMS y convenciones de dominio
cambian por región).
**PRUEBA**: por UF: FP=0, revisión ≤30%.
**SI una UF falla**: las diferencias se corrigen en patrones de
plataforma/registro (F3), NUNCA con reglas por municipio.

### F7.P2 — Rollout por UF bajo señal
**ACCIONES**: habilitar la cola demand-driven (F4.P3) para las UFs validadas;
la señal nacional (QD cubre ~grandes municipios; bancas nacionales; radar)
decide el orden. Auditoría muestral por lote (como F2.P8) hasta n≥300
confirmaciones nacionales sin FP.
**PRUEBA**: cobertura de certames ACTIVOS ≥ radar de referencia en las UFs
habilitadas, con el estándar de evidencia intacto.
**SI la cuota/costo muerde**: ya se decidió pagado en F2.P7 (~$151 el país);
el freno realista es la cola humana → priorizar por población/actividad.

# FASE 8 — Producto (portal/app)

Sigue `MANUAL_APP.md` (etapas 0-9 con stack y trampas). Gates de este plan:

### F8.P1 — DB + API de catálogo (arranca al cerrar F2.P8)
**ACCIONES**: Postgres+PostGIS con el modelo canónico; loader idempotente
CSV→DB; `GET /certames` con filtros. Solo filas `confirmado` se sirven.
**PRUEBA**: la API devuelve los confirmados reales de RS con provenance.

### F8.P2 — Web pública consultable
**PRUEBA**: buscar "Canoas, superior, R$3.000+" devuelve certames reales con
fuentes enlazadas; páginas server-rendered con schema.org/JobPosting.

### F8.P3 — Cuentas + perfil (LGPD)
**PRUEBA**: registro <2 min; export/borrado self-service; política publicada.

### F8.P4 — Matching + alertas (requiere F5 y F6)
**ACCIONES**: matcher por reglas explícitas (escolaridade+geo+salario);
eventos F5/F6 → notificaciones idempotentes; email digest + web push;
instantáneo solo para deadline/apertura que matchea.
**PRUEBA**: usuario de prueba recibe alerta real <48h de la publicación
real; 0 alertas falsas en el período de prueba.

### F8.P5 — Beta cerrada RS
**PRUEBA**: ~50 usuarios reales; métricas de Etapa 8 del MANUAL_APP
(retención s4, % alertas abiertas, reportes de error/1.000 vistas). El botón
"reportar error" alimenta la cola de revisión y el fixture envenenado.
**SI la retención no justifica expandir**: iterar producto con RS antes de
gastar en F7 — los datos de RS ya sostienen el aprendizaje.

---

## Decisiones pendientes registradas (dueño: Luis)
1. ~~Feed-tag oficial como índice válido (F2.P2)~~ — **RESUELTA 12-jul: SÍ**.
2. Las 6 confirmaciones con `requiere_revision_humana` (F2.P5): ¿match válido?
3. Autorización del holdout 50 (F2.P6) y de la corrida 497 (F2.P8).
4. Presupuesto pagado para producción tras la sonda de cuota (F2.P7).
5. F2.P4: aceptar R4 (30/36, 0 FP) pese a que R5 cayó a 26/36 por varianza,
   o definir una política adicional antes de pasar a la adjudicación F2.P5.

## Orden de ejecución recomendado (si nada bloquea)
F2.P1 → F2.P2 → F2.P3 → F2.P4 → (paralelo: F2.P7, F3.P1, F3.P2, F4.P1) →
F2.P5 → F2.P6 → F2.P8 → F3.P3-P4 → F4.P2-P3 → F5 → F6 (F6.P1-P2 pueden
adelantarse con bancas) → F7 → F8 (F8.P1-P3 arrancan tras F2.P8).
