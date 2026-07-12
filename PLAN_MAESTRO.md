# PLAN MAESTRO — Concursos Tracker

**Versión**: 1.0 (12-jul-2026) · **Autoridad**: este documento manda sobre
ROADMAP.md (historial) y complementa CLAUDE.md (reglas operativas).

**Cómo usar este documento (para cualquier agente/IA/humano)**: cada PASO
tiene `ENTRADA` (prerrequisito verificable), `ACCIONES` (qué hacer, con
comandos/archivos exactos), `PRUEBA` (criterio de éxito medible) y `SI FALLA`
(la rama a tomar). Ejecutar en orden; no saltar gates. Antes de tocar código,
leer CLAUDE.md (intocables, reglas) y `MANUAL_IMPLEMENTACION.md`
(la arquitectura de 4 planos y el embudo convergente §6b).

---

## 0. Estado verificado al 12-jul-2026 (punto de partida)

- Motor V2 (adjudicación IA): `scripts/fase2_municipios/v2/` — certificador A
  + fiscal B + juez C sobre Gemini flash-lite free, citas literales
  verificadas por código, gate estructural, registro de dominios, render-once
  SPA. Suite: **419 tests verdes** (`pytest scripts/fase2_municipios/v2`).
- Última corrida golden (R3): `staging/fase2_v2/eval/golden36_fable_20260712_r3/`
  → **22/36 match vs golden, 0 FP** (los 14 differ son abstenciones), paid=0,
  84 llamadas free, perfil 2.33 calls/unidad, 22.4K tokens/unidad, 11.6s/unidad.
- Comparación controlada (`semantic_matrix_r3_20260712/`): sobre evidencia
  idéntica, V2 acierta 22/23 vs 2/23 de las heurísticas V1.
- Fixture y oracle: `url_map_golden_fixture_20260712.csv` (36 URLs verificadas),
  `golden_oracle_manifest_20260712.json`, diff razonado en
  `url_fixture_diff_20260712.csv`.
- Advertencia estadística vigente: 0-FP demostrado solo sobre n=22
  confirmaciones (cota superior 13.6% al 95%). No prometer 0-FP a escala
  hasta cumplir F1.P6.
- Análisis de escala (síntesis Opus 12-jul): demand-driven >> backfill
  (~$15-20/año y ~12 h-humano/año vs 208 días free / ~200 h); techo free
  EMPÍRICO ~125 req/día (contradice blogs de 1.500) — verificar antes de
  planear; Tier 1.5 nunca corrido a escala; B/C sin capturas netas en 133
  unidades pero nunca enfrentaron veneno.

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
  --credentials-file /home/orion/.hermes/gemini_concursos.env --seed <YYYYMMDDNN>'
# matriz semántica ex-post (sin modelo)
wsl.exe ... -m scripts.fase2_municipios.v2.eval.semantic_comparison --run-dir <RUN> --golden ... --output-dir <NUEVO>
```

## Reglas transversales (aplican a TODOS los pasos)

- **R-T1 · Protocolo STOP por FP**: si una auditoría encuentra UNA confirmación
  con URL/decisión equivocada → parar la fase, abrir análisis de causa raíz,
  corregir de forma GENERAL (nunca hardcode municipal), añadir el caso al
  fixture envenenado, re-correr el gate de la fase. Ningún avance con FP
  abierto.
- **R-T2 · No enseñar etiquetas**: el golden/manifiesto jamás se lee en
  runtime; las URLs son input de adquisición, las decisiones se ganan
  adjudicando contenido vivo. Aprendizajes entran SOLO como hechos curados
  con provenance (registro de dominios, v2/memory promotion por humano).
- **R-T3 · Corridas congeladas**: durante una corrida de evaluación no se
  toca código ni skills; output SIEMPRE a directorio nuevo en
  `staging/fase2_v2/eval/`; corridas previas no se sobrescriben; seed nuevo
  por corrida; sin promoción al CSV canónico desde corridas de evaluación.
- **R-T4 · RED→GREEN**: todo fix lleva primero un test que falla; la suite
  completa v2 debe quedar verde antes de cualquier corrida.
- **R-T5 · Paid=0 y grounding=off** en evaluación salvo autorización explícita
  de Luis. Producción a escala usará pagado (decisión F1.P7).
- **R-T6 · Intocables**: ver CLAUDE.md; incluye verdict_extract.py,
  cascade_municipios.py, golden CSV, corridas congeladas.
- **R-T7 · Memoria**: al cerrar cada paso, actualizar el checkpoint del
  proyecto (memoria del agente o HANDOFF.md) con: qué se hizo, números, y el
  paso siguiente.

---

# FASE 1 — Cerrar RS con V2 (el gate de todo lo demás)

### F1.P1 — Fixes mecánicos de evidencia/fetch
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
**SI FALLA** (algún fix rompe otra cosa): revertir SOLO ese fix, documentar en
el paso, continuar con los demás — ninguno depende de otro.

### F1.P2 — Política de índice (2 reglas generales en skills)
**ENTRADA**: decisión de Luis registrada (estado 12-jul: contenedor mixto =
SÍ, ya implícito en README "content over slug"; feed-tag = pendiente de
confirmación explícita, default recomendado SÍ).
**ACCIONES**: en `skills/fase2-resource-certifier/SKILL.md` añadir (general,
sin municipios): (a) contenedor oficial mixto cuenta como índice del bucket
SI contiene ítems citables del bucket; (b) feed/tag oficial agregador cuenta
como listado (una noticia individual sigue rechazada); (c) sección vacía
NUNCA se confirma. Espejo en fiscal B (nuevo motivo de acusación si el
contenedor no tiene ítems del bucket).
**PRUEBA**: suite verde; los sha256 de skills re-registrados en memoria.
**SI FALLA** (Luis dice NO a feed-tag): solo (a) y (c); Canoas/PS queda como
revisar legítimo esperado y el techo de F1.P4 baja en 1.

### F1.P3 — Fixture envenenado (mide a B/C y blinda el 0-FP)
**ENTRADA**: F1.P1 hecho.
**ACCIONES**: construir `staging/fase2_v2/eval/fixture_envenenado_v1.csv` con
15-20 unidades donde la URL es PLAUSIBLE pero INCORRECTA, tomadas de páginas
reales de RS fuera del golden: (tipos obligatorios) página de licitações,
noticia individual de un concurso, índice del bucket contrario, índice de
OTRO municipio, página de detalle de un edital, sección cultural
(soberanas), soft-404 con 200. Correr V2 sobre ellas (mismo comando patrón,
golden sintético con expectativa `rechazo/revisar` por unidad).
**PRUEBA (doble)**: (1) **FP=0**: ninguna unidad envenenada termina
`confirmado` — si una pasa, R-T1 (STOP) y el hueco es del gate/skills, no de
B. (2) Conteo de capturas de B/C: ¿cuántos venenos detuvo B que A no detuvo
solo?
**SI B/C capturan ≥1 neto**: se quedan como están. **SI capturan 0**: activar
modo slim por defecto (A+gate; B/C solo en desacuerdo o muestreo aleatorio
10%), manteniendo el código de B/C intacto para re-activación.

### F1.P4 — R4 contra el golden 36
**ENTRADA**: F1.P1-P3 verdes.
**ACCIONES**: corrida R4 (comando patrón, `--output-dir ...golden36_<fecha>_r4`,
seed nuevo). Generar `v2_only_differential` + `semantic_comparison`.
**PRUEBA**: (1) FP=0 (differ solo abstenciones) — si no, R-T1. (2) match ≥
30/36. (3) Cada differ restante clasificado y con dueño: `revisar legítimo`
(adjudicado por Luis como correcto) o `bug` (→ fix general → R5).
**SI match < 30**: diagnosticar por clase con los artefactos (observability),
UNA ronda más de fixes generales, R5. Si R5 sigue < 30: presentar a Luis la
matriz y decidir si el techo restante es aceptable (los "revisar honestos"
pueden ser correctos — el golden marca 7 unidades requiere_revision_humana).

### F1.P5 — Adjudicación humana de cierre RS-golden
**ENTRADA**: R4/R5 con FP=0.
**ACCIONES**: Luis revisa (a) las confirmaciones nuevas (muestra o todas), (b)
los differ marcados `revisar legítimo`, (c) las 6-7 unidades
requiere_revision_humana que V2 confirmó con URL exacta (¿match válido o
sobre-confirmación? — decisión de oráculo pendiente del 12-jul).
**PRUEBA**: acta escrita (doc en staging) con cada differ adjudicado.
**SI Luis encuentra un FP**: R-T1.

### F1.P6 — Holdout 50 (la prueba de generalización; NO ejecutar antes de P5)
**ENTRADA**: acta de P5; autorización explícita de Luis.
**ACCIONES**: seleccionar 50 municipios de los 497 NO presentes en el golden,
estratificados por plataforma (atende/elotech/govbr/custom proporcional).
Descubrimiento SIN curación manual: registro + Tier 1.5 + cascada → V2.
Luis construye la verdad DESPUÉS de la corrida (a ciegas) solo para las
unidades que V2 confirmó + una muestra de las abstenciones.
**PRUEBA**: FP=0 en confirmadas (hard); precisión ≥95%; tasa de revisión
(abstenciones) ≤ 30% del total.
**SI FP>0**: R-T1 + el caso entra al fixture envenenado. **SI revisión >30%**:
el hueco es descubrimiento → reforzar F2 (Tier 1.5/registro) antes de re-intentar.

### F1.P7 — Sonda de cuota + decisión free/pagado
**ENTRADA**: cualquier momento desde F1.P4.
**ACCIONES**: quemar ~200 llamadas en 1 día con 1 key free (corrida real o
replay) y registrar dónde aparece `quota_429` (`approx_rpd` ya se mide).
**PRUEBA**: techo real documentado (¿~125 o ~1.500 RPD?).
**DECISIÓN**: si techo <300 RPD → producción con API pagada (~$151 todo
Brasil, trivial); free queda solo para desarrollo.

### F1.P8 — Corrida 497 RS completa
**ENTRADA**: F1.P6 verde.
**ACCIONES**: correr los 497 (menos golden) por lotes de ~50; auditoría
muestral humana de 50-100 confirmaciones POR LOTE con intervalo de confianza.
**PRUEBA**: FP=0 en cada muestra; al acumular n≥300 confirmaciones auditadas
sin FP, la cota superior al 95% baja de ~1% → recién ahí se puede DECIR
"cero FP" públicamente.
**SALIDA DE FASE 1**: mapa RS completo con provenance → alimenta el Plano B.

# FASE 2 — Descubrimiento industrializado (el hueco)

### F2.P1 — Tier 1.5 como proponente a escala
**ENTRADA**: F1.P4 (no requiere holdout).
**ACCIONES**: correr las sondas por plataforma (`cascade` Tier 1.5, ya
implementado y golden-limpio) sobre los 497 de RS; registrar % que propone
candidata y % que coincide con el confirmado de F1.
**PRUEBA**: WRNG=0 (ninguna propuesta contradice un confirmado auditado);
cobertura de propuesta ≥45% (la concentración de plataformas RS es ~50%).
**SI WRNG>0**: el patrón de esa plataforma se corrige o se degrada a
"propone-pero-marca-revisar"; jamás se parchea por municipio.

### F2.P2 — Registro dominio↔IBGE nacionalizable
**ACCIONES**: tabla por código IBGE (5.570) con dominio oficial; fuentes en
orden: registro RS existente → Wikidata P856 (dump/SPARQL) → sondeo DNS de
convenciones por UF → verificación de vida+identidad (script, sin IA).
Toda fila con `fuente` y `fecha`.
**PRUEBA**: RS ≥95% cubierto automático (hoy el slug puro ya da ~95% en RS);
muestra manual de 30 filas nacionales sin error.

### F2.P3 — Goldens chicos en 2 UFs diversas
**ACCIONES**: Luis (o revisión humana guiada) construye golden de 10-20
municipios en 2 UFs no-sur (p.ej. BA/PA); repetir F1.P4-P6 en miniatura.
**PRUEBA**: mismas métricas que F1. **SI fallan**: las diferencias van a
patrones de plataforma/registro (F2.P1-P2), NUNCA a reglas por municipio.

# FASE 3 — Señal de actividad (habilita demand-driven)

### F3.P1 — Prototipo Querido Diário sobre RS
**ACCIONES**: consultar la API de QD por municipio RS con términos
("concurso público", "processo seletivo", "edital de abertura") sobre los
últimos 60 días; cruzar con los certames reales conocidos (bancas fase 1 +
confirmados F1).
**PRUEBA**: recall ≥80% de los certames conocidos del período; precisión de
la señal ≥70% (mención real de certame, no ruido).
**SI QD no cubre suficiente RS**: complementar señal con bancas (fase 1 ya
las monitorea) + radar Ache/PCI como descubrimiento (nunca autoridad), y
documentar cobertura por fuente.

### F3.P2 — Cola demand-driven
**ACCIONES**: job que convierte señal→(municipio, bucket) a verificar; si no
hay URL confirmada fresca (TTL 90 días) → dispara descubrimiento F2 + V2; si
hay → dispara lectura de monitoreo (F4).
**PRUEBA**: en 30 días, ≥90% de los certames RS señalados terminan con
fuente confirmada o en cola humana con SLA <7 días.

# FASE 4 — Monitoreo (Plano B del MANUAL_IMPLEMENTACION)
Scheduler + diff de contenido normalizado + clasificación IA-con-citas de
cambios → eventos tipificados. **PRUEBA de fase**: 30 días detectando los
editais nuevos reales de RS (recall-check vs radar) con 0 avisos falsos.

# FASE 5 — Extracción de certames (Plano C)
Empezar por bancas; golden de extracción de 30 editais anotados a mano;
campos núcleo ≥90%, 0 campos inventados (sin evidencia ⇒ null+revisar).
Resolver identidad multi-fuente (clave órgão+edital+año+banca) antes de
mostrar nada al usuario.

# FASE 6 — Producto
Seguir `MANUAL_APP.md` (etapas 0-9). Los sprints 1-2
(DB+API) pueden arrancar en paralelo desde el cierre de F1.P8 con el CSV
canónico; las alertas dependen de FASE 4.

---

## Decisiones pendientes registradas (dueño: Luis)
1. Feed-tag oficial como índice válido (F1.P2) — default recomendado: SÍ.
2. Las 6 confirmaciones con `requiere_revision_humana` (F1.P5): ¿match válido?
3. Autorización del holdout 50 (F1.P6) y de la corrida 497 (F1.P8).
4. Presupuesto pagado para producción tras la sonda de cuota (F1.P7).

## Orden de ejecución recomendado (si nada bloquea)
F1.P1 → F1.P2 → F1.P3 → F1.P4 → (paralelo: F1.P7, F2.P1) → F1.P5 → F1.P6 →
F1.P8 → F2.P2-P3 → F3 → F4 → F5 → F6.
