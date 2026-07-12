# Manual de implementación — Concursos Tracker

**Fecha**: 12-jul-2026 · **Estado de referencia**: fase 2 RS con V2 (22/36 golden, 0 FP)
**Propósito de este documento**: el mapa completo y exacto desde el estado actual
hasta el producto final, con criterios de salida medibles por fase. Sustituye la
vaguedad del ROADMAP anterior; no sustituye a CLAUDE.md (reglas operativas) ni a
ARCHITECTURE.md (detalle técnico de fase 2).

---

## 1. El producto final (el norte)

Portal/app de avisos de concursos públicos y processos seletivos de todo Brasil:

- El usuario registra su **perfil**: escolaridade, profissão, cidade + radio de
  distancia (o ciudades a las que se mudaría), salario mínimo aceptable.
- El portal muestra los certames **elegibles para ese perfil** y envía **alertas
  de ciclo de vida**: nuevo edital, retificação, inscripciones por cerrar,
  convocação, homologação, nomeação.
- Cada certame debe estar **completo**: todos sus documentos (edital, anexos,
  cronograma, retificações, resultados, convocações) enlazados y su línea de
  tiempo reconstruida, sin importar en cuál fuente se publicó cada pieza.

**La tesis del proyecto** (correcta): el foso competitivo es el motor de datos,
no la interfaz. Obtener, verificar y mantener fresca esta información a escala
de 5.570 municipios es lo difícil; el front-end es trabajo estándar.

**El estándar de calidad que ya adoptamos y NO se negocia**: cero falsos
positivos con evidencia citada. Un aviso falso (URL equivocada, concurso
inexistente, plazo mal leído) destruye la confianza del usuario que quizá pagó
una inscripción o planeó una mudanza. Todo lo que el sistema afirme debe poder
mostrar su evidencia literal (el mismo principio de citas verificadas de V2 se
extiende a la extracción y a los avisos).

---

## 2. Arquitectura: cuatro planos

El sistema completo son cuatro planos independientes con contratos claros entre
sí. Hoy existe el Plano A (parcial); los otros tres están especificados aquí.

```
PLANO A — DESCUBRIMIENTO   ¿DÓNDE se publica?   (urls estables por fuente)
PLANO B — MONITOREO        ¿CUÁNDO hay algo nuevo?  (lecturas periódicas + diff)
PLANO C — EXTRACCIÓN       ¿QUÉ dice?           (certames estructurados + timeline)
PLANO D — PRODUCTO         ¿A QUIÉN le sirve?   (matching + alertas + portal)
```

### Plano A — Descubrimiento de fuentes

Objetivo: para cada (municipio, tipo) y cada banca, conocer la(s) URL(s)
estables donde se publica. Cuatro familias de **fuentes de verdad que
CONVERGEN** — ninguna es "la" autoridad de todo; cada una es autoritativa
para una porción distinta de los hechos del certame y se corroboran entre sí
(acuerdo entre fuentes independientes sube confianza; divergencia manda a
revisión). La autoridad se asigna POR TIPO DE HECHO, no por fuente (matriz
fuente × evento):

1. **Bancas organizadoras** — la fuente más RICA del ciclo activo
   (edital→provas→resultado) para los certames que usan banca (mayormente
   concursos grandes). **La mayoría de los PSS y algunos concursos nunca
   pasan por banca.** *Estado: fase 1 hecha para RS.*
2. **Prefeituras / órganos** — el publicador legal; muchas veces la ÚNICA
   fuente para PSS; autoritativa para convocação/nomeação y seguimiento
   municipal. *Estado: fase 2 RS en curso; motor V2 funcionando (ver §4).*
3. **Diários oficiales** — el registro con valor legal de los actos
   administrativos. *Integración clave pendiente: **API de Querido Diário**
   (querido-diario.ok.org.br): texto completo de diários municipales,
   buscable por término y municipio.*
4. **Radar (Ache Concursos, PCI, QConcursos)** — solo descubrimiento y
   auditoría de cobertura, jamás prueba final.

El resolvedor de identidad del Plano C es la pieza que materializa la
convergencia: une las menciones multi-fuente en un solo Certame, y cada
hecho (fecha, cargo, documento, evento) conserva la provenance de la fuente
que lo prueba.

Componentes ya construidos y reutilizables a escala nacional:
- Cascada de 5 tiers (barato→caro) para descubrir la URL municipal.
- **V2 (A/B/C)**: adjudicación por IA con citas literales verificadas, fiscal
  adversarial de FP y gate estructural — el "verificador de URLs por IA" que
  pide el proyecto, ya independiente de heurísticas V1.
- **Registro versionado de dominios oficiales** (hechos host↔municipio con
  provenance humana) + autoridad por cadena de redirect: resuelve dominios no
  estándar (pmaratiba, pmpf, prefeitura.poa.br) sin hardcodes.
- Render-once para SPAs (atende.net, elotech) preservando snapshot.
- Metodología de evaluación: golden manual como oracle + holdout, matriz
  V2-vs-golden, comparación semántica controlada.

### Plano B — Monitoreo (no existe aún; primera pieza nueva a construir)

Objetivo: dado el mapa de URLs del Plano A, detectar novedades a tiempo.

- **Scheduler de lecturas** por fuente con frecuencia adaptativa: bancas con
  certames activos = diaria; página municipal con actividad = diaria/semanal;
  municipio históricamente quieto = quincenal. (5.570 municipios × 2 buckets es
  ~11K fetches por vuelta — trivial en volumen si se escalona.)
- **Detección de cambio**: comparar snapshot nuevo vs anterior (hash de
  contenido normalizado del listado, no del HTML crudo — los sitios meten ruido).
  Solo si cambió, pasa a clasificación.
- **Clasificación del cambio** (IA con citas, mismo patrón V2): ¿apareció un
  certame nuevo, un documento nuevo de un certame conocido, o ruido? Salida:
  eventos tipificados que alimentan el Plano C.
- **Detector de URL rota/migrada**: si una URL estable empieza a dar
  404/redirect/vacío persistente, se re-dispara el descubrimiento del Plano A
  para ese municipio (esto responde a "las URLs pueden cambiar").
- **Querido Diário como radar de actividad**: consulta periódica por términos
  ("concurso público", "processo seletivo", "edital de abertura") por
  municipio; sirve para (a) priorizar qué municipios mirar ya, (b) auditar
  cobertura (si el diário menciona un certame que no tenemos, hay un hueco),
  (c) capturar eventos administrativos (homologação, nomeação) que no siempre
  llegan al sitio municipal.

### Plano C — Extracción y consolidación (no existe aún; el corazón del producto)

Objetivo: convertir páginas y PDFs en **certames estructurados** con timeline.

- **Identidad del certame** (el problema difícil escondido): el mismo concurso
  aparece en la banca, la prefeitura, el diário y el radar con nombres
  distintos. Clave natural: (órgão, nº de edital, año, tipo) + banca cuando
  existe. Se necesita un **resolvedor de identidad** que una menciones
  multi-fuente en una sola entidad Certame. Sin esto no hay "portal completo",
  hay duplicados. Mismo estándar: solo se fusiona con evidencia citada; en
  duda, quedan separados con flag de posible duplicado.
- **Extracción del edital (PDF)**: cargos, vagas, escolaridade por cargo,
  remuneração, taxa, cronograma (fechas de inscripción/prueba), ámbito
  geográfico. IA con citas al texto del PDF (offsets/página), validación de
  tipos (fechas, moneda) por código. Los campos de filtro del producto salen
  EXACTAMENTE de aquí — sin esto no hay filtros de escolaridade/salario.
- **Timeline de eventos**: cada documento/publicación se tipifica (abertura,
  retificação, prorrogação, resultado, convocação, homologação, nomeação) y se
  cuelga del certame con fecha y fuente. La banca da el ciclo activo; la
  prefeitura y el diário dan la cola administrativa.

### Plano D — Producto

Perfil de usuario, matching (elegibilidad por escolaridade/cargo + geografía
por radio + salario), alertas (nuevo match, cambios de estado de certames
seguidos, deadline de inscripción), portal de consulta. Trabajo estándar de
producto EXCEPTO dos cosas que hay que diseñar con el motor: (1) el matching
geográfico necesita municipio→coordenadas (IBGE lo da) y (2) las alertas deben
heredar el estándar de evidencia: cada aviso enlaza al documento fuente.

---

## 3. Modelo de datos canónico

Entidades mínimas (los nombres importan menos que las claves y relaciones):

| Entidad | Clave | Campos núcleo |
|---|---|---|
| `Municipio` | código IBGE | nombre, UF, lat/lon |
| `Orgao` | municipio + nombre normalizado | esfera (municipal/estatal), tipo |
| `Banca` | slug | urls base, patrón de portal |
| `Fuente` | url canónica | tipo (banca/prefeitura/diario/radar), municipio/banca, bucket, estado (activa/rota/migrada), evidencia de autoridad, fecha verificación, **provenance** (quién/cómo se confirmó) |
| `Certame` | (orgao, nº edital, año, tipo) | banca, estado del ciclo, ámbito geográfico |
| `Documento` | certame + url/hash | tipo (edital/anexo/retificação/...), fecha pub, fuente |
| `Evento` | certame + tipo + fecha | tipificación del ciclo, documento que lo prueba |
| `Cargo` | certame + nombre | vagas, escolaridade, remuneração, cidade de lotação |
| `Mencion` | fuente + certame-candidato | crudo pre-resolución de identidad |

Regla transversal: **toda fila afirmativa lleva provenance** (fuente, fecha,
evidencia citada, quién adjudicó: IA-con-citas o humano). Es lo que ya hace V2
con las URLs y el registro de dominios; se extiende igual a certames y eventos.

---

## 4. Estado actual honesto (12-jul-2026)

**Existe y funciona:**
- Fase 1: crawlers de bancas RS (`scripts/fase1_bancas/`).
- Fase 2 RS: cascada de descubrimiento + **motor V2** de adjudicación
  (certificador A / fiscal B / juez C sobre Gemini free, citas literales
  verificadas, gate estructural, independencia de heurísticas V1 certificada
  por revisión adversarial). Último resultado: 22/36 unidades del golden
  coinciden, **cero falsos positivos**, y en comparación controlada sobre
  evidencia idéntica V2 acierta 22/23 vs 2/23 del clasificador heurístico V1.
- Registro de dominios oficiales + autoridad por redirect + render-once SPA.
- Metodología de evaluación completa (golden manual, fixture QA, matrices).
- CSV canónico RS de producción (fase 2 v1) con auditorías previas.

**No existe todavía (en orden de construcción):**
1. Cierre fase 2 RS: llevar el golden a verde estable, holdout de 50, corrida
   de los 497 con V2.
2. Plano B completo (scheduler, diff, clasificador de cambios, QD radar).
3. Plano C completo (identidad de certame, extracción de editais, timeline).
4. Expansión nacional (§6).
5. Plano D (producto).

---

## 5. Roadmap por fases con criterios de salida

Cada fase tiene un criterio de salida MEDIBLE. No se pasa a la siguiente sin
cumplirlo (el patrón golden→holdout se repite en cada plano).

**F2 — Cerrar descubrimiento RS (en curso)**
- V2 verde en golden 36 (match o discrepancia adjudicada por humano como
  correcta), 0 FP.
- Holdout de 50 municipios nunca vistos: precisión de confirmados ≥95%, 0 FP.
- Corrida completa 497 con V2; cola humana esperada ~15-20%.
- *Salida: mapa de URLs RS con provenance, listo para monitoreo.*

**F3 — Monitoreo RS (Plano B, primera versión)**
- Scheduler + diff + clasificador de cambios sobre las URLs confirmadas de RS
  + bancas fase 1.
- Querido Diário integrado como radar (consulta por municipio RS).
- *Salida: en 30 días de operación, detectar los editais nuevos reales de RS
  (validar contra Ache/PCI como recall-check) con 0 avisos falsos.*

**F4 — Extracción de certames (Plano C núcleo)**
- Empezar por BANCAS (estructura más regular, ciclo completo): extraer certame
  + cargos + cronograma de N editais reales con citas.
- Golden de extracción: ~30 editais anotados a mano (campos + offsets).
- *Salida: ≥90% de campos núcleo correctos contra el golden de extracción; 0
  campos inventados (todo campo sin evidencia = null + flag revisión).*

**F5 — Identidad multi-fuente + timeline**
- Resolvedor de identidad banca↔prefeitura↔diário sobre RS.
- Eventos administrativos desde Querido Diário + páginas municipales.
- *Salida: para ~50 certames RS, timeline completa reconstruida y auditada.*

**F6 — Expansión nacional (demand-driven, NO exhaustiva)**
- **No mapear 5.570 municipios de golpe.** Orden de ataque:
  a. Bancas nacionales (cubren la mayoría de concursos grandes con ciclo
     completo) — extender fase 1.
  b. Municipios con actividad DETECTADA (vía bancas + Querido Diário + radar):
     descubrir su URL municipal on-demand con la cascada+V2. La demanda real
     dicta la cobertura; el long tail se llena solo con el tiempo.
  c. Golden chico por estado (~15-25 municipios) antes de habilitar
     confirmación automática en ese estado (los CMS municipales cambian por
     región: atende.net en el sur, otros en el nordeste).
- Infra: corridas desde Brasil (geo-block ya documentado en RUNBOOK), límites
  de cuota por proveedor, registro de dominios creciendo vía promoción humana
  y redirect-evidence.
- *Salida: cobertura de certames ACTIVOS nacional ≥ radar de referencia, con
  la precisión del estándar V2.*

**F7 — Producto (Plano D)**
- Modelo de datos servido por API; app con perfil, filtros, alertas.
- *Salida: beta con usuarios reales en 1-2 estados.*

---

## 6. Decisiones estratégicas ya tomadas (y por qué)

1. **Precisión > cobertura, siempre.** Un tracker con 70% de cobertura y cero
   errores es un producto; uno con 95% y avisos falsos es una demanda de
   reembolso. (Validado: la disciplina V2 lo hace alcanzable con IA barata.)
2. **Authority-first.** Radar descubre, autoridad prueba. Ache Concursos es
   brújula y auditor de recall, nunca fuente final.
3. **IA adjudica contenido, código verifica hechos.** (Directiva 12-jul,
   implementada.) Escala a extracción: la IA lee el edital, el código valida
   tipos y exige citas; lo no probado queda null.
4. **Demand-driven para escalar.** La actividad detectada prioriza el
   descubrimiento; nunca "mapear todo Brasil" como prerequisito.
5. **El humano promueve patrones, el sistema no aprende solo.** Aprendizajes
   (dominios, patrones de portal, reglas de índice) entran como hechos curados
   con provenance (registro + v2/memory promotion), jamás auto-aprendidos del
   oráculo — eso preservaría la validez de los holdouts.
6. **Definición de índice válido** (aclarada 12-jul, alineada con README
   "content over slug"): contenedor oficial mixto CUENTA si contiene ítems
   citables del bucket; feed/tag oficial agregador CUENTA como listado; una
   noticia individual NO; una sección vacía NO se confirma.

---

## 6b. La arquitectura convergente del descubrimiento (12-jul, post-análisis de escala)

Resultado de cruzar el diseño desde-cero con lo ya construido y con el
análisis numérico (3 investigadores + síntesis adversarial). El sistema final
es UN embudo donde cada pieza existente tiene rol asignado:

```
SEÑAL DE ACTIVIDAD (nuevo)      Querido Diário / diários / bancas / radar
   │  "¿en qué municipio hay concurso/PSS AHORA?"  → prioriza TODO lo demás
   ▼
E0 REGISTRO dominio↔municipio (existe en RS; nacionalizar con IBGE+Wikidata)
   ▼
E1 PATRONES POR PLATAFORMA (Tier 1.5: existe, golden-limpio, NUNCA corrido
   a escala → activarlo como PROPONENTE; ~50% de cobertura en RS)
   ▼
E2 COLA LARGA (cascada tiers 1-2 + agente navegador para sitios custom)
   ▼
E3 COMPUERTA V2 (existe y validada: A + citas verificadas por código + gate
   + reparación; B/C pendientes de ganarse el puesto vs fixture envenenado)
   ▼
E4 REGISTRO VIVO con provenance + TTL + re-check bajo señal (las URLs se
   pudren: 50% de drift observado en semanas)
   ▼
E5 HUMANO por apalancamiento: aprueba PATRONES (1 revisión ≈ cientos de
   municipios) y adjudica solo abstenciones; auditoría muestral por lote
   (50-100 confirmaciones) hasta certificar 0-FP estadísticamente (hoy n=22)
```

Roles y estado:
| Pieza | Estado | Rol en el sistema final |
|---|---|---|
| Señal de actividad nacional | **FALTA** | Decide QUÉ verificar y CUÁNDO (demand-driven: ~$15-20/año, ~12 h-humano/año vs 208 días/200 h del backfill free) |
| Registro dominios + IBGE | Existe (RS) | Fundación E0; nacionalizar antes de salir de RS |
| Tier 1.5 patrones | Existe, inactivo | Proponente principal (E1) — activar YA |
| Cascada tiers 0-4 | Existe (V1) | Fallback de cola larga (E2), no el camino principal |
| V2 A+gate+reparación | Existe, validado | Compuerta única de publicación (E3) — intocable |
| Fiscal B / juez C | Existe | En observación: medir vs fixture envenenado; recortar a A+gate si no atrapan nada |
| Golden + holdout por UF | Existe (RS) | Gate de entrada a cada estado nuevo (2-3 UFs diversas antes de generalizar) |
| Fixes evidencia/fetch | Parcial | Cuello real del yield (cap snapshot, timeouts, retry) |

Decisiones cerradas por el análisis: (a) demand-driven, no backfill; (b) el
modelo barato + arnés de verificación le gana al modelo grande (2.7× costo
sin ganancia); (c) el free tier NO es plan de producción (techo empírico ~125
req/día vs 1.500 de blogs — verificar antes de planear; pagado cuesta ~$151
todo Brasil, trivial); (d) "poco revisar" honesto = ~20-25% de unidades en
backfill, ~12 h/año en demand-driven.

## 7. Riesgos reales del "el resto es solo hacerlo bonito"

Para calibrar expectativas — estos no son bloqueantes pero no son gratis:

- **Frescura**: un aviso de edital 5 días tarde vale poco. El Plano B necesita
  SLA de detección (24-48h para fuentes activas).
- **Duplicados**: sin resolvedor de identidad, el portal mostrará el mismo
  concurso 3 veces (banca + prefeitura + diário). Es EL bug visible nº1 de los
  agregadores.
- **PDF hell**: editais escaneados (imagen), tablas de cargos multiformato.
  Presupuestar OCR + extracción con citas por página, y aceptar cola humana.
- **Infra Brasil**: geo-block ya conocido; el monitoreo continuo necesita el
  runner local/VPS brasileño del RUNBOOK como pieza permanente, no ocasional.
- **Notificaciones**: deliverability (email/push/WhatsApp) y preferencias son
  un subsistema en sí; no diseñarlo al final.
- **Legal**: datos públicos oficiales — OK; respetar robots/ToS de radar
  privados (usarlos como auditoría, no re-publicar su contenido).

---

## 8. Cómo se conecta el trabajo de HOY con este manual

El trabajo actual (V2, golden RS, registro, render) construye el músculo
central que TODO lo demás reutiliza: *afirmar solo con evidencia citada,
verificada por código, con fiscal adversarial y oráculo humano*. La fase 2 no
es "encontrar URLs de RS": es el banco de pruebas donde ese músculo se
entrena y se mide antes de apuntarlo a la extracción (F4), a los eventos (F5)
y a Brasil entero (F6).
