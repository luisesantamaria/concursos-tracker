# SPEC de diseño — productor de cassette golden Fase 2 V2

Estado: diseño, no implementación. Este documento define el artefacto que debe
producirse para el replay existente en HEAD `7aee723`. No autoriza cambios al
runner, a V1 ni a las skills canónicas.

## 1. Contrato schema-1 obligatorio

### 1.1 Naturaleza y envelope del corpus

El cassette existente no es una grabación HTTP. Es un único documento JSON
UTF-8 cargado desde una ruta inyectada; el adapter lee el archivo completo y
devuelve sus casos (`scripts/fase2_municipios/v2/eval/golden_runner.py:101-115`).
No existen en schema 1 método HTTP, headers, body, status, encoding, redirect
chain ni una clave de match de requests.

El objeto raíz tiene exactamente dos campos consumidos:

- `schema_version`: debe valer `1`. La constante es `SCHEMA_VERSION = 1` y
  cualquier otro valor aborta como versión no soportada
  (`scripts/fase2_municipios/v2/eval/golden_runner.py:35`,
  `scripts/fase2_municipios/v2/eval/golden_runner.py:105-114`).
- `cases`: lista de unidades. Si no es lista, el replay aborta
  (`scripts/fase2_municipios/v2/eval/golden_runner.py:112-115`).

El fixture confirma el envelope real en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:1-4`.
No hay rechazo de propiedades adicionales en el envelope: son ignoradas por el
consumidor. El productor schema 1, sin embargo, debe limitarse a los campos aquí
documentados para no crear un segundo contrato implícito.

### 1.2 Unidad `(municipio, bucket)`

Cada elemento de `cases` debe ser un objeto. Sus campos de unidad son:

- `municipio`: string de hasta 200 caracteres, sin controles salvo LF, tab y CR.
- `bucket`: uno de `concurso_publico` o `processo_seletivo`.
- `v1`: objeto de la capa V1.
- `v2`: objeto de snapshot, propuestas A/B y cassette de C.

La validación de tipo, límites y bucket está en
`scripts/fase2_municipios/v2/eval/golden_runner.py:139-146` y
`scripts/fase2_municipios/v2/eval/golden_runner.py:491-506`. La identidad real
es `(golden_evaluator.muni_key(municipio), bucket)`; una segunda unidad con la
misma identidad normalizada aborta como duplicada
(`scripts/fase2_municipios/v2/eval/golden_runner.py:497-510`). Por cada fila del
golden se exigen ambos buckets; una unidad ausente aborta explícitamente
(`scripts/fase2_municipios/v2/eval/golden_runner.py:518-530`). El fixture muestra
las dos unidades en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:4-6`
y
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:86-89`.

### 1.3 Capa V1

`v1` debe ser un objeto. Los campos que deben existir y tener tipo válido para
que el replay no aborte son:

- `decision`: string de hasta 100 caracteres.
- `url`: string de hasta 2.000 caracteres; puede ser `""` para revisión o
  resultado negativo.
- `evidence`: objeto con los cuatro strings obligatorios:
  - `snapshot_ref`, hasta 4.000 caracteres;
  - `authority`, hasta 200;
  - `identity`, hasta 200;
  - `reason`, hasta 4.000.

El consumidor lo exige en
`scripts/fase2_municipios/v2/eval/golden_runner.py:446-456`; la función común de
evidencia exige los cuatro campos en
`scripts/fase2_municipios/v2/eval/golden_runner.py:292-311`. `snapshot_ref` es un
string opaco para este replay: el consumidor no recalcula su hash.

El dominio de `decision` es: `indice_oficial`,
`indice_oficial_combinado`, `portal_externo_oficial`, `revisar`,
`nao_encontrado`, o una decisión terminada en `_rechazado`/`_rechazada`
(`scripts/fase2_municipios/v2/eval/golden_runner.py:41-45`,
`scripts/fase2_municipios/v2/eval/golden_runner.py:149-156`). El fixture muestra
la forma afirmativa completa en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:7-16`
y la forma de revisión en el mismo archivo, líneas 89-98.

### 1.4 Capa V2: evidencia y snapshot compartido por A/B/C

`v2` debe ser un objeto. Su `evidence` reutiliza los cuatro strings obligatorios
de la capa V1 y añade `sources`, una lista no vacía de como máximo 32 objetos
(`scripts/fase2_municipios/v2/eval/golden_runner.py:314-321`). Cada source debe
tener:

- `source_id`: string, máximo 200;
- `url`: string, máximo 2.000;
- `retrieved_at`: string ISO-8601 parseable;
- `content`: string, máximo 200.000 caracteres.

La construcción y validación están en
`scripts/fase2_municipios/v2/eval/golden_runner.py:324-353`. El fixture muestra
los campos en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:17-31`.

`v2.citations` es una lista obligatoria; puede ser vacía. Cada elemento debe ser
objeto y, para ser válido, contener `source_id`, `start`, `end` y `quote`. Los
offsets son obligatorios porque el consumidor usa `require_offsets=True`, y la
cita se verifica contra el snapshot
(`scripts/fase2_municipios/v2/eval/golden_runner.py:356-385`). Ejemplo:
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:32-39`;
lista vacía válida en líneas 99-115.

`v2.candidate` es opcional y puede ser `null`, como muestra
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:114-115`.
Si es objeto, `candidate_id` y `url` deben ser strings; el consumidor además lee
`decision`, `bucket`, `authority`, `identity`, `evidence_state` y `source_kind`
(`scripts/fase2_municipios/v2/eval/golden_runner.py:388-426`). Estos seis últimos
no provocan por sí solos una excepción de schema si faltan, pero una confirmación
sin valores coherentes no supera el gate final. Para una unidad afirmativa el
productor debe emitir el objeto completo mostrado en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:40-49`.

### 1.5 Capa A/B: propuestas

`v2.proposal_a` y `v2.proposal_b` deben ser objetos; si cualquiera no lo es, el
replay aborta como cassette A/B ausente
(`scripts/fase2_municipios/v2/eval/golden_runner.py:458-474`). La forma válida de
cada propuesta contiene:

- `decision`;
- `bucket`;
- `candidate_id`;
- `resource_url`;
- `citations`, lista o tupla de objetos;
- `reason`.

Los seis campos y sus tipos son el contrato de `DecisionProposal.from_mapping`
(`scripts/fase2_municipios/v2/agents/orchestration.py:38-83`). El fixture muestra
A en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:50-64`
y B en líneas 65-79.

Distinción de obligatoriedad: el envelope A/B debe existir y ser objeto para no
abortar. Si el objeto existe pero le falta uno de sus seis campos,
`DecisionProposal` lo rechaza y la orquestación degrada la salida a revisión; no
es un cassette completo ni publicable aunque el proceso no lance excepción.

### 1.6 Capa C: cassette del juez

`v2.judge_response` debe existir y ser un objeto. El model adapter lo obtiene de
la unidad y aborta si falta o no es mapping
(`scripts/fase2_municipios/v2/eval/golden_runner.py:118-124`). La respuesta válida
de C tiene exactamente:

- `decision`: `aceptar_A`, `aceptar_B` o `revisar`;
- `reason`: string no vacío.

El schema cerrado del juez está en
`scripts/fase2_municipios/v2/agents/schemas.py:89-100`; el fixture lo materializa
en
`scripts/fase2_municipios/v2/eval/tests/fixtures/synthetic_replay_corpus.json:80-83`.
Aunque A y B coincidan y C no sea invocado, el consumidor actual sigue exigiendo
que `judge_response` sea un objeto. En ese caso sus campos no llegan a validarse
por el juez, pero el productor debe emitir igualmente la forma válida cerrada
para evitar cassettes cuyo comportamiento dependa de si A/B divergen.

### 1.7 Resumen de obligatoriedad

Abortan inmediatamente si faltan o tienen tipo inválido: versión, `cases`, unidad
objeto, `municipio`, `bucket`, objetos `v1`/`v2`, campos V1, evidencia base,
`v2.evidence.sources`, campos de cada source, `v2.citations`, candidate no nulo
sin `candidate_id`/`url`, envelopes `proposal_a`/`proposal_b`, y
`judge_response` mapping.

No necesariamente abortan, pero hacen la unidad inválida o la degradan a
`revisar`: campos semánticos incompletos del candidate, propuesta mapping sin los
seis campos válidos y respuesta C semánticamente inválida cuando C llega a ser
invocado. El productor debe considerar cualquiera de estos casos como fallo de
producción, no como cassette listo.

## 2. Qué aporta run497 y qué falta

La inspección local establecida de `/home/orion/.hermes/run497_corpus` produjo:
618 archivos JSON válidos de 618, 319 municipios normalizados, 618 unidades sin
duplicados y cobertura de 18 de los 24 municipios golden. Su información útil
para la capa histórica V1 es: municipio, bucket, URL, título, texto, anchors,
decisión capturada y evidencia ya transformada.

run497 es **parcialmente reconstruible**, no un cassette schema 1 completo:

- aporta material histórico para sembrar V1 en 18/24 municipios;
- no contiene snapshot/propuestas A/B ni `judge_response` C de V2;
- no contiene transporte crudo: método, headers, cookies, request body, response
  status, response body crudo, encoding o redirects;
- su decisión `confirmar` no identifica por sí sola cuál de las decisiones
  discretas afirmativas de schema 1 corresponde;
- faltan seis municipios golden completos.

Los seis municipios, derivados comparando `muni_key` del header `municipio` del
golden contra los 618 JSON, son:

1. Araricá
2. André da Rocha
3. Santa Maria
4. Viamão
5. São Leopoldo
6. São Pedro do Sul

No se debe fabricar una unidad para ellos ni traducir ausencia a
`nao_encontrado`: el runner exige evidencia por municipio/bucket y aborta ante
una unidad ausente (`scripts/fase2_municipios/v2/eval/golden_runner.py:518-528`).

## 3. Estrategias

### A. Full-live

Consiste en ejecutar una producción completa controlada: obtener la capa V1
desde el pipeline original y correr V2 una vez con Gemini free-only,
grounding/tools `None`, capturando snapshot, candidate, A, B y C antes de
proyectarlos a schema 1.

- **Viabilidad:** alta para obtener un corpus íntegro y contemporáneo de 24
  municipios por dos buckets.
- **Qué falta hoy:** el modo live actual solo valida el contrato y delega una
  llamada opaca a `request_adapter.request()`; no graba cassette
  (`scripts/fase2_municipios/v2/eval/golden_runner.py:660-662`).
- **Riesgo FP:** bajo si toda captura incompleta, bloqueo o error termina en
  revisión/fallo de producción; mayor si se interpreta un bloqueo como ausencia.
- **Esfuerzo:** mayor consumo de red/modelo y necesidad de envolver V1 sin tocarlo,
  pero menor complejidad de reconciliar evidencia histórica con actual.

### B. Builder desde run497

Consiste en proyectar los JSON históricos a `v1` y no ejecutar V2.

- **Viabilidad:** parcial para evidencia histórica V1 de 18/24.
- **Faltantes:** A/B/C completo, seis municipios, y resolución segura de
  `confirmar` hacia el dominio discreto del replay.
- **Riesgo FP:** alto si el builder infiere autoridad, identidad o tipo de índice
  a partir de resultados transformados o históricos.
- **Esfuerzo:** bajo, pero no produce un corpus consumible completo.

**B sola es insuficiente.** El replay exige siempre `v2`, sources, citas,
propuestas A/B y un mapping `judge_response`
(`scripts/fase2_municipios/v2/eval/golden_runner.py:458-482`), y exige una unidad
para ambos buckets de cada municipio golden
(`scripts/fase2_municipios/v2/eval/golden_runner.py:518-530`). run497 no puede
satisfacer ninguna de esas dos condiciones por sí solo.

### C. Híbrido

Consiste en sembrar V1 desde run497 donde la traducción sea demostrable, completar
V1 de los seis municipios faltantes mediante una ejecución controlada del
pipeline original y hacer **una sola pasada live V2** para capturar A/B/C reales
de las 48 unidades golden.

- **Viabilidad:** alta, condicionada a que cada unidad V1 reconstruida conserve
  decisión discreta, URL, autoridad, identidad, motivo y snapshot ref sin
  inferencias.
- **Qué falta:** captura V1 específica de los seis municipios y revisión manual o
  evidencia adicional para cualquier `confirmar` histórico que no pueda mapearse
  de forma inequívoca.
- **Riesgo FP:** menor que B porque V2 se ejecuta sobre evidencia actual; cualquier
  ambigüedad de V1 queda incompleta/revisión en vez de promoverse.
- **Esfuerzo:** medio; reutiliza 18/24 históricos y limita el modelo a una pasada
  V2, pero requiere reconciliación explícita de las capas.

### Recomendación

Se recomienda **C, híbrido fail-closed**, con A como fallback por unidad. La
razón es que conserva el valor verificable de run497 sin fingir que contiene
A/B/C, limita V2 a una pasada live y obliga a capturar aparte los seis huecos.
Una unidad run497 que no permita reconstruir todos los campos V1 sin inferencia
se retira del camino builder y se produce por A; nunca se completa con defaults
afirmativos. El corpus solo se publica cuando existen las 48 unidades completas.

## 4. Sourcing de V1 para el diferencial

Para los 18 municipios cubiertos, la fuente inicial es cada JSON run497 de su
bucket. El builder debe extraer únicamente hechos presentes: municipio, bucket,
URL, decisión/evidencia capturada y contenido transformado. Debe validar la
unidad con `muni_key` y no usar igualdad textual simple, porque esa es la misma
clave del replay (`scripts/fase2_municipios/v2/eval/golden_runner.py:501-510`).

La proyección V1 requiere:

- una decisión del dominio cerrado, no el string genérico `confirmar`;
- URL exacta o vacía según decisión;
- `snapshot_ref`, `authority`, `identity` y `reason` como strings.

Si esos campos no pueden justificarse desde run497 y los artefactos originales
de V1, el builder no debe inferirlos. Esa unidad pasa al fallback full-live/V1
controlado.

Para Araricá, André da Rocha, Santa Maria, Viamão, São Leopoldo y São Pedro do
Sul se debe ejecutar el entrypoint original en un entorno interceptado y capturar
su `FinalDecision` y evidencia seleccionada. La salida CSV no debe usarse para
inventar evidence: el productor debe capturar el objeto interno por bucket. La
capa V1 queda congelada antes de ejecutar V2 y permanece separada en el
diferencial.

El golden es verdad de evaluación, no fuente para construir decisiones V1. Usar
sus URLs como respuesta del productor contaminaría el diferencial.

## 5. Plan de test offline

Los tests del productor deben ejecutarse bajo el guard de red de la suite V2 y
con adapters inyectados. No se usa SDK real, Gemini real, DNS ni HTTP externo.

### Fakes

- `FakeFetchAdapter`: devuelve snapshots y resultados predeterminados por unidad.
- `FakeV1Source`: devuelve una capa V1 completa o un error explícito.
- `FakeCertifierA` y `FakeProsecutorB`: devuelven propuestas cerradas.
- `FakeJudgeC`: devuelve `aceptar_A`, `aceptar_B` o `revisar` sin modelo.
- `FixedReplayClock`: reloj fijo ya contemplado por el runner
  (`scripts/fase2_municipios/v2/eval/golden_runner.py:89-98`).

### Asserts mínimos

1. Dos producciones con los mismos fakes y seed generan bytes idénticos.
2. El JSON producido tiene `schema_version: 1` y 48 unidades únicas ordenadas.
3. Cada unidad contiene V1, snapshot V2, citations, candidate/null, A, B y C.
4. El cassette producido es consumido por `GoldenDifferentialRunner.run_replay`
   sin completar campos durante replay.
5. Una unidad golden ausente produce error con municipio/bucket, no un artefacto
   marcado como listo.
6. Una unidad parcial —por ejemplo V1 presente pero A/B/C ausentes— produce error
   de producción y no se persiste como cassette final.
7. Una respuesta C ausente o no mapping falla antes de publicar, coherente con
   `scripts/fase2_municipios/v2/eval/golden_runner.py:118-124`.
8. Dos unidades que colisionan tras `muni_key` fallan explícitamente, coherente
   con `scripts/fase2_municipios/v2/eval/golden_runner.py:507-510`.
9. Cualquier acceso no registrado en un fake levanta inmediatamente un error
   cerrado como `UnexpectedExternalCall`; no hace fallback a red o SDK.
10. Cualquier llamada a socket, HTTP, Gemini SDK o resolución de una credencial
    real falla inmediatamente. El contador del guard debe permanecer en cero en
    la ruta feliz y registrar el intento en el test negativo.
11. El test negativo instala bombas en transport, entorno y socket y demuestra
    que ninguna excepción se convierte en `nao_encontrado` o `listo`.

## 6. Reproducibilidad y seguridad

### Determinismo

- Ordenar unidades por `(muni_key(municipio), bucket)` antes de serializar.
- Usar un seed explícito y persistirlo solo en metadata de ejecución separada;
  schema 1 no tiene campo `seed`.
- Emitir JSON UTF-8, `allow_nan=False`, claves ordenadas, separadores compactos y
  LF final, siguiendo la canonicalización ya usada por el runner
  (`scripts/fase2_municipios/v2/eval/golden_runner.py:218-228`).
- Emitir siempre `schema_version: 1`; una versión distinta es incompatible.
- Ordenar `sources` por `source_id`, citas por `(source_id,start,end,quote)` y
  mantener A/B en sus slots, sin intercambiarlos por orden lexicográfico.
- Congelar reloj y seed en tests; en producción, registrar `retrieved_at` real con
  timezone y conservar exactamente el contenido usado para offsets de citas.

### Seguridad de la pasada live

La pasada live debe usar exclusivamente credencial free y `tools=None`. Antes de
persistir cualquier material se aplica redacción determinista:

- nunca persistir API keys, tokens, ADC ni valores de entorno;
- eliminar/redactar `Authorization`, `Proxy-Authorization`, `Cookie` y
  `Set-Cookie`;
- redactar parámetros sensibles de query y formularios con un placeholder
  estable por nombre, sin guardar el valor ni un hash reversible;
- eliminar headers volátiles o identificadores de sesión;
- evitar que excepciones o logs incluyan secretos.

Schema 1 no contiene campos para headers/cookies: por tanto, estos datos no se
añaden al cassette. La redacción se realiza antes de derivar `url`, `content`,
motivos o logs persistentes. Si una URL sensible no puede sanitizarse sin cambiar
la identidad funcional de la evidencia, la unidad falla y pasa a revisión; no se
persiste el secreto.

## 7. Edge cases

### Golden sin run497

Los seis municipios enumerados requieren V1 controlado y V2 live. Su ausencia no
es `nao_encontrado`; es `missing replay evidence/cassette` hasta completar ambos
buckets (`scripts/fase2_municipios/v2/eval/golden_runner.py:523-528`).

### Bucket faltante

Aunque exista el otro bucket del municipio, el corpus es incompleto y no se
publica. El runner itera los dos buckets por fila golden
(`scripts/fase2_municipios/v2/eval/golden_runner.py:518-530`).

### Unidad parcial

V1 sin V2, sources, A/B o C es error. El productor escribe primero a un artefacto
temporal y solo publica tras validar el corpus completo con el replay.

### Versión incompatible

Todo valor diferente de `schema_version: 1` aborta
(`scripts/fase2_municipios/v2/eval/golden_runner.py:105-114`). No se hace upgrade
implícito.

### Municipio duplicado tras normalización

Se rechaza la segunda unidad con el municipio original y bucket en el mensaje;
no se fusiona evidencia (`scripts/fase2_municipios/v2/eval/golden_runner.py:501-510`).

### Respuesta C ausente

Falla como cassette de juez ausente incluso si A/B parecen iguales
(`scripts/fase2_municipios/v2/eval/golden_runner.py:118-124`). El productor debe
capturar una respuesta válida o una respuesta válida de revisión, no omitirla.

### Bloqueo 403/429/Cloudflare/geo

Se clasifica como captura bloqueada/incompleta. No se toma el HTML de challenge
como evidencia oficial, no se promueve a confirmación y no se interpreta como
ausencia. La unidad queda en revisión o la producción falla de forma explícita,
con error libre de secretos. Solo un snapshot de contenido oficial verificable
puede alimentar sources y citas.

## Criterio de salida del futuro productor

El productor estará listo únicamente cuando pueda generar un JSON schema 1 con
48 unidades únicas y completas, validarlo inmediatamente mediante el replay
actual bajo guard fail-closed y obtener bytes deterministas en una repetición con
los mismos inputs congelados. Hasta entonces cualquier salida es staging parcial,
no cassette golden publicable.
