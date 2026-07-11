# Fase 2 V2 AI-first — recon y blueprint de staging

Estado: blueprint del paso 1. La V2 es totalmente paralela y aditiva. **V1 no se
modifica**: `scripts/fase2_municipios/cascade_municipios.py`,
`offline_validate.py`, sus tests, los contratos de `scripts/eval/`, el CSV
canónico y `authority_first/data/` permanecen fuera del alcance de V2.

## Superficie exacta de V1

### Entrada, CLI y salida

El entrypoint es `scripts/fase2_municipios/cascade_municipios.py:3204`. Su CLI
define `--golden` (líneas 3208-3210), `--all` (3211-3212), `--municipio`
(3213-3214), `--output` (3215-3216), `--model` (3217-3218),
`--gemini-free-first` (3219-3221), `--no-playwright` (3222-3223), `--timeout`
(3224), `--limit` (3225-3226), `--letras` (3227-3229), `--append`
(3230-3233), `--skip-existing` (3234-3238) y el flag de compatibilidad
`--grounded-verify` (3239-3242). Los cinco flags pedidos explícitamente son:

- `--all`: procesa los 497 municipios RS.
- `--letras`: filtra por iniciales sin acentos.
- `--append`: mezcla por municipio con un CSV existente.
- `--skip-existing`: omite filas con ambos buckets confirmados y preserva el
  bucket ya confirmado cuando solo uno lo está.
- `--output`: selecciona la ruta; por defecto `data/cascade_output.csv`.

`OUTPUT_FIELDS` está en `cascade_municipios.py:2996-3003` y el escritor en
`cascade_municipios.py:3050-3084`. Las columnas, en orden, son: `uf`,
`municipio`, `site_base`, `url_concursos`, `confianza_concursos`,
`url_processos_seletivos`, `confianza_processos`, `urls_extras_concursos`,
`urls_extras_processos`, `tier_concursos`, `tier_processos`, `method`, `razao`,
`notes`, `checked_at`.

### Módulos y cadena contractual

La superficie V1 bajo `scripts/fase2_municipios/` está concentrada en:

- `cascade_municipios.py`: cascade, captura, adjudicador central, selector,
  cierre y CLI.
- `offline_validate.py`: replay desde evidencia congelada, sin red; su docstring
  y contrato están en líneas 1-11 y la entrada en 115.
- `tests/test_batch_snapshot_seam.py`, `test_candidate_decision_chain.py`,
  `test_contrato_estructural.py` y `test_producer_hydration_seam.py`.

El contrato estructural consumido por el adjudicador procede de
`scripts/eval/verdict_extract.py`: `build_candidate_record()` llama a
`verdict.evaluate_candidate_contract()` en `cascade_municipios.py:811-877`.
No hay hoy un módulo separado llamado `adjudicador`; el adjudicador central está
embebido en esa función y en las funciones de contrato importadas.

La cadena es:

1. `CandidateRecord` (`cascade_municipios.py:510-581`) congela identidad,
   autoridad, rol, estado de evidencia, bucket, decisión, provenance y el
   `EvidenceSnapshot` exacto.
2. Tier 3 es solo selector: `tier3_classify_and_pick()`
   (`cascade_municipios.py:2100-2186`) elige un `candidate_id` existente entre
   records elegibles; no reclasifica.
3. `SelectedResource` (`cascade_municipios.py:584-590`) conserva la instancia
   exacta y el bucket.
4. `derive_final_decision()` (`cascade_municipios.py:2038-2096`) produce el
   `FinalDecision` (`cascade_municipios.py:593-602`) sin refetch ni nueva
   adjudicación.

### Gemini en V1

V1 no tiene un cliente reutilizable fuera de `cascade_municipios.py`: el wrapper
HTTP local es `gemini_post()` (`1567-1585`), apoyado por
`_gemini_post_once()` (`1527-1536`) y `_gemini_post_paid()` (`1543-1564`). La
clave gratuita se obtiene de la variable **`GEMINI_API_KEY_FREE`**
(`1413-1415`); el camino histórico/pago usa `GEMINI_API_KEY` (`1398-1400`).

Tier 2 usa Google Search grounding **encendido** mediante
`"tools": [{"google_search": {}}]` en `tier2_grounded_search()`
(`1588-1612`), `tier2_find_site_grounded()` (`1681-1703`) y
`tier2_directed_bucket_search()` (`1759-1779`). Tier 3 usa grounding **apagado**:
su payload no contiene tools y solicita JSON por `responseMimeType` en
`2100-2174`. V1 puede caer de free a pago (`1575-1584`); esa conducta queda
expresamente prohibida en V2.

## Skills y referencias canónicas

Archivos canónicos, y únicamente éstos:

- `skills/fase2-resource-certifier/SKILL.md`: certifica una superficie estable
  por autoridad, identidad, rol, bucket y evidencia congelada; recibe
  `EvidenceSnapshot`/provenance y produce JSON estricto, citas, decisión,
  posible tool request y propuesta de aprendizaje (líneas 11-42, 208-244).
- `skills/fase2-fp-prosecutor/SKILL.md`: auditor adversarial independiente;
  recibe snapshot, propuesta y citas, prueba acusaciones de falso positivo y
  devuelve `sustain`, `block`, `needs_tool` o `review` con evidencia
  (líneas 11-39, 54-65).
- `skills/fase2-conflict-judge/SKILL.md`: resuelve solo conflictos entre
  certificador y fiscal desde snapshot, salidas y citas; devuelve `confirm`,
  `reject`, `request_tool` o `review` (líneas 11-24, 36-41, 75-77).
- `skills/fase2-resource-certifier/references/schema.json`: objeto JSON con
  `$schema`, `title`, `type`, `additionalProperties`, `required` y `properties`.
  **Es un JSON Schema Draft 2020-12 para `Fase2CertifierOutput`; no es un schema
  aplicable a los otros tres references** (líneas 1-48).
- `portal_families.json`: objeto JSON `{version: int, families: list}` con 15
  familias; cada entrada tiene `id` y metadatos operativos opcionales.
- `failure_modes.json`: objeto JSON `{version: int, failure_modes: list}` con 14
  modos; cada entrada tiene `id`, `fp`, `action` y, a veces, `contrast`.
- `casebook.jsonl`: JSON Lines, un objeto por línea, 19 líneas/19 registros.
  Cada registro contiene `case_id`, `municipio`, `family`, `expected`, `bucket`,
  `facts` (lista de strings) y `lesson`.

`jsonschema` no está instalado en `.venv`, y aun si lo estuviera el schema no
aplica a las otras referencias. Por tanto, el loader de este paso valida cada
archivo con contratos estructurales mínimos propios; no instala dependencias.

## Tests e intérprete

El framework es pytest 9.1.1 en `.venv`; los tests V1 viven en
`scripts/fase2_municipios/tests/`. No existe configuración pytest ni markers
`network`/`integration` en la superficie inspeccionada. Tampoco hay llamadas
reales a `requests`, sockets o Playwright en los tests; los seams externos se
mockean. La suite recolecta 88 tests.

Comando unitario/offline exacto:

```bash
env -u GEMINI_API_KEY -u GEMINI_API_KEY_FREE -u GEMINI_API_KEY_PAID \
  -u GEMINI_FREE_FIRST .venv/bin/python -m pytest -q scripts/fase2_municipios/tests
```

`CLAUDE.md:19-24` prescribe crear y activar `.venv`. La ruta comprobada es
`<repo>/.venv/bin/python`; actualmente informa Python 3.14.4, aunque AGENTS.md
declara Python 3.12 como versión objetivo. Esta divergencia se reporta, no se
corrige en este paso.

## Layout V2 implementado

En disco, las rondas 1 a 6 crean:

```text
scripts/fase2_municipios/v2/
├── __init__.py
├── loader.py
├── gemini/
│   ├── __init__.py
│   ├── client.py
│   ├── schema_validation.py
│   └── tests/
│       └── test_client.py
├── agents/
│   ├── __init__.py
│   ├── base.py
│   ├── certifier.py
│   ├── judge.py
│   ├── orchestration.py
│   ├── prosecutor.py
│   ├── schemas.py
│   ├── tools.py
│   └── tests/
│       ├── test_agents.py
│       └── test_orchestration.py
├── memory/
│   ├── __init__.py
│   ├── _jsonl.py
│   ├── models.py
│   ├── store.py
│   ├── capture.py
│   ├── audit.py
│   ├── promotion.py
│   ├── audit_cli.py
│   └── tests/
│       └── test_memory.py
├── ratelimit/
│   ├── __init__.py
│   ├── limiter.py
│   └── tests/
│       └── test_limiter.py
├── snapshot/
│   ├── __init__.py
│   ├── snapshot.py
│   └── tests/
│       └── test_snapshot.py
└── tests/
    └── test_loader.py
```

El rate limiter se implementó en ronda 2, el cliente Gemini free-only en ronda 3,
el snapshot en ronda 4, el framework/certificador/fiscal en ronda 5, el juez
cerrado con orquestación A/B/C en ronda 6 y el staging externo append-only en
ronda 7.

## Contratos de interfaz de alto nivel

Firmas de diseño; loader y rate limiter ya están implementados, las restantes
continúan como contratos futuros:

```python
def find_repo_root(start: Path | None = None, *, max_parents: int = 8) -> Path: ...

def load_canonical_resources(
    *, repo_root: Path | None = None,
    skills_dir: Path | None = None,
    references_dir: Path | None = None,
) -> CanonicalResources: ...

@dataclass(frozen=True)
class LimiterConfig:
    rpm: int = 15
    tpm: int = 250_000
    rpd: int | None = None
    rpd_policy: Literal["raise", "block"] = "raise"

class ProjectRateLimiter:
    def acquire(self, estimated_tokens: int) -> Reservation: ...
    def reconcile(self, reservation: Reservation, actual_tokens: int) -> None: ...

class Transport(Protocol):
    def generate(
        self, model: str, contents: object, config: Mapping[str, object],
    ) -> RawResponse: ...

class StructuredGeminiClient:
    def generate_structured(
        self, contents: object, *, estimated_tokens: int,
        config_overrides: Mapping[str, object] | None = None,
    ) -> object: ...

@dataclass(frozen=True)
class EvidenceSource:
    source_id: str
    url: str
    retrieved_at: datetime
    content: str
    content_sha256: str

@dataclass(frozen=True)
class EvidenceSnapshot:
    sources: tuple[EvidenceSource, ...]
    snapshot_sha256: str

@dataclass(frozen=True)
class Citation:
    source_id: str
    start: int
    end: int
    quote: str

class ResourceCertifier:
    def certify(self, *, snapshot: EvidenceSnapshot, task: str) -> AgentRunResult: ...

class FalsePositiveProsecutor:
    def audit(
        self, *, snapshot: EvidenceSnapshot,
        certifier_output: Mapping[str, object],
    ) -> AgentRunResult: ...

class ConflictJudge:
    async def judge(
        self, snapshot: EvidenceSnapshot, proposal: CertifierVerdict,
        prosecution: ProsecutionVerdict,
    ) -> JudgeVerdict: ...

class CaptureSink:
    def capture(self) -> CaptureReport: ...  # write-only, candidate pre-bound

# Solo módulo/CLI humano separado; el pipeline no importa estas interfaces.
def read_learning_events(path: Path) -> tuple[LearningEvent, ...]: ...
def append_promotion_event(
    path: Path, *, learning_id: str, actor: str, promoted_at: datetime,
) -> PromotionEvent: ...
```

## Rate limiter (implementado ronda 2)

La implementación real vive en `scripts/fase2_municipios/v2/ratelimit/`. Usa
`LimiterConfig`, inmutable, con defaults 15 RPM, 250.000 TPM, RPD ilimitado y
`rpd_policy="raise"`. `ProjectRateLimiter.acquire(estimated_tokens)` reserva en
una cola FIFO antes de llamar al proveedor y devuelve un `Reservation`;
`Reservation.reconcile(actual_tokens)` —o el método equivalente del limiter—
reemplaza la estimación por el consumo real. RPM/TPM son ventanas deslizantes de
60 segundos con reloj monotónico inyectable; RPD hace rollover a medianoche UTC
con reloj UTC inyectable.

El default RPD `raise` produce `QuotaExhaustedError` auditable y evita bloquear
una corrida durante casi 24 horas. `block` queda disponible como decisión
explícita. El singleton es compartido y thread-safe solo dentro de un proceso,
alcance suficiente para certificador/fiscal/juez en esta fase. Los hooks
`_synchronize_cross_process_locked()` y `_publish_cross_process_locked()` son el
seam pendiente para coordinación futura con file-lock; ronda 2 no implementa
estado cross-proceso.

## Cliente Gemini free-only (ronda 3)

La implementación vive en `scripts/fase2_municipios/v2/gemini/`. El cliente
genérico `StructuredGeminiClient` recibe un `Transport`, limiter, modelo y
`response_schema`; no conoce roles ni loader. `Transport.generate(model,
contents, config)` no maneja credenciales y devuelve `RawResponse` con texto y
tokens de prompt/candidatos/total. `build_gemini_client()` es el único choke
point V2: recibe un transporte inyectado o resuelve el entorno en cada llamada,
sin caché al importar. Solo `RealGeminiTransport(api_key)` recibe la key
explícita, importa perezosamente `google.genai` y construye el SDK con
`api_key=...` y `vertexai=False`; Vertex, ADC y gcloud no tienen fallback.

`resolve_free_api_key()` usa exclusivamente `GEMINI_API_KEY_FREE`. Antes de leer
esa variable, inspecciona por membresía —nunca por valor y en precedencia fija—
`GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_API_KEY` y `GEMINI_API_KEY`.
Cualquier presencia, incluso con whitespace, produce
`UnauthorizedCredentialError`; ausencia o valor vacío/whitespace de la key free
produce `MissingFreeApiKeyError`. No existe default implícito ni fallback.

Grounding está apagado por construcción: overrides superiores usan allowlist y
`_guard_grounding()` recorre mappings, listas, dataclasses y objetos tipados para
rechazar en cualquier nivel `tools`, `google_search`,
`google_search_retrieval`, `grounding`, `retrieval` y equivalentes normalizados.
La infracción falla antes de reservar cuota o invocar el transporte.

Structured output es siempre `application/json`. El cliente acepta cualquier
schema compatible con el subconjunto Draft 2020-12 implementado de forma
fail-closed por `validate_json_schema()`. JSON no parseable y objeto que incumple
schema generan `SchemaValidationError` con `reason="invalid_json"` y
`reason="schema_mismatch"`, respectivamente, sin incluir respuesta o prompt.
Como el loader no ofrece validación pública de instancias, la factoría
`build_certifier_client()` reutiliza su entrypoint público exacto
`load_canonical_resources()` para cargar `Fase2CertifierOutput` desde
`references/schema.json`; el cliente genérico permanece desacoplado.

Los defaults configurables de `RoleModels` son certificador y fiscal
`gemini-3.1-flash-lite`, y juez `gemini-3.5-flash`. La inyección de Transport y
la construcción separada de clientes son el seam para sesiones y prompts
independientes del fiscal; el cableado ocurre en la ronda de agentes.

Cada intento hace `acquire(estimated_tokens)` antes del transporte y reconcilia
en `finally` el total real reportado antes de parsear/validar, de modo que incluso
una respuesta JSON/schema inválida contabiliza tokens. `max_attempts=3` significa
un intento más dos reintentos, exclusivamente para `TransientTransportError`;
cada intento reserva y reconcilia por separado. Credenciales, grounding, schema,
usage y `QuotaExhaustedError` nunca se reintentan ni disparan fallback.

Usage ausente, negativo, no entero o con `total != prompt + candidates` produce
`UsageInconsistencyError`: no se asume cero, no se reintenta y la reserva
estimada permanece cargada. Logging estructurado incluye solo modelo, intento,
tipo de error y contadores de tokens; excluye key, contents, prompt, respuesta y
texto de excepciones.

## EvidenceSnapshot + citas verificables (ronda 4)

La implementación autocontenida vive en
`scripts/fase2_municipios/v2/snapshot/` y no importa V1. `EvidenceSource` es un
dataclass frozen con `source_id`, URL, timestamp timezone-aware recibido del
caller, contenido renderizado crudo y SHA-256 calculado sobre `content.encode(
"utf-8")`. No consulta reloj, filesystem ni red. Se permite contenido vacío: su
hash es el SHA-256 estándar de `b""`; es evidencia reproducible, aunque no puede
sostener una cita no vacía.

`build_snapshot()` rechaza IDs vacíos, duplicados o ajenos a la allowlist
discreta de roles oficiales V2 (`main`, `main_content`, `title`, `chrome`,
`page`); el mismo tripwire se aplica al entrar citas. Reconstruye defensivamente
cada fuente, recalcula hashes, ordena por `source_id` y expone una tupla.
`snapshot_sha256` es SHA-256 del JSON UTF-8 compacto de pares ordenados
`[source_id, content_sha256]`. `EvidenceSnapshot` vuelve a comprobar orden,
unicidad y ambos niveles de hash en `__post_init__`; `get_source()` hace lookup
sin exponer un dict mutable.

`Citation` exige siempre `source_id`, `start`, `end` y `quote`. El snapshot no
transforma el contenido: su normalización es identidad sobre el `str` crudo.
Por tanto, `start`/`end` son índices de caracteres Python sobre ese mismo `str`,
no offsets de bytes. `verify_citation()` comprueba exactamente el slice, sin
normalizar Unicode, mayúsculas ni espacios. Los offsets siempre refieren al
contenido crudo y requieren `content[start:end] == quote`; no existe un
normalizador alternativo inyectable. Fuente inexistente o vacía, quote ausente,
límites inválidos o incoherencia offset/quote fallan cerrados.

`verify_all()` evalúa el lote completo y, si hay fallos, emite
`CitationBatchVerificationError` con índices, source IDs, razones y previews de
quote limitados a 48 caracteres; nunca vuelca el contenido fuente. Cuando todas
pasan devuelve un reporte frozen con índices y fuentes verificadas. Los agentes
reciben el snapshot solo para lectura: toda afirmación
material deberá aportar una `Citation` y superar `verify_all()` antes de poder
confirmarse, auditarse o juzgarse.

### Mapeo conceptual V1 → V2, sin acoplamiento

- V1 define su `EvidenceSnapshot` frozen en
  `scripts/fase2_municipios/cascade_municipios.py:153-173`, con HTML, texto,
  título, URL solicitada/final, status, productor, estado y links. V2 conserva la
  idea de evidencia renderizada separada de su productor, pero reduce cada
  unidad verificable a `EvidenceSource` con contenido crudo, URL e identidad.
- V1 congela listas de links en tuplas (`:167-173`); V2 generaliza la
  deep-immutability mediante una tupla ordenada de fuentes y no expone mappings
  mutables.
- V1 calcula un fingerprint SHA-1 del payload completo en `:776-791`. V2 no usa
  esa función: calcula SHA-256 por contenido y un SHA-256 agregado reproducible
  ligado a `source_id`.
- V1 construye/copía snapshots desde `Page` en `:947-976`. V2 no conoce `Page`,
  requests ni Playwright: `build_snapshot()` recibe exclusivamente strings y
  timestamps ya proporcionados, y nunca adquiere contenido.
- Título, status, links, requested/final URL y `evidence_state` no se importan
  como campos implícitos en ronda 4. Si un agente necesita citarlos, deben entrar
  explícitamente como contenido de una fuente futura o mediante un contrato
  versionado; no se infieren desde V1.

## Framework de agentes + tool loop (ronda 5)

### Lectura canónica y contratos de rol

La factoría carga las skills con `load_canonical_resources()` y no modifica ni
copia su semántica. El certificador recibe el expediente/snapshot municipal y
produce `Fase2CertifierOutput`: dimensiones, decisión, confianza, `citations`,
razón, tool request y propuesta de aprendizaje. Su schema canónico define cita
como `{dimension, quote, source_field}` y exige citas literales para toda
confirmación. El fiscal recibe el mismo snapshot, la salida propuesta del
certificador y sus citas; rehace el análisis de forma adversarial y devuelve
`sustain`, `block`, `needs_tool` o `review`, con acusaciones probadas,
descartadas/no resueltas y evidencia. Nunca recibe el historial o razonamiento
operativo del certificador.

Las skills no delimitan una subsección separada de system prompt. Por eso se
elimina únicamente el frontmatter YAML de metadata y se inyecta **verbatim todo
el cuerpo Markdown restante** de `fase2-resource-certifier/SKILL.md` o
`fase2-fp-prosecutor/SKILL.md`, respectivamente. No se resume ni reinterpreta el
texto canónico. Cada rol usa un `StructuredGeminiClient`, contents, modelo y
system prompt separados; ambos modelos default son `gemini-3.1-flash-lite`.

### Tool loop de aplicación

No se usa function-calling nativo porque el guard free-only prohíbe la clave
`tools` en config junto con grounding. Cada respuesta del modelo es un
`Fase2AgentStepV2` plano: `action` es `tool` o `final`, con propiedades
opcionales `tool`, `args` y `output`. Primero se valida con
`validate_json_schema()`; Python exige después `tool+args` y ausencia de output
para `tool`, o `output` y ausencia de tool/args para `final`. JSON/schema o
invariante inválida produce `InvalidAgentStepError` inmediato, sin reintento.

El loop comienza con system prompt, tarea autorizada, protocolo AgentStep e
inventario local `list_sources`. Cada turno, incluido `final`, consume un paso.
Defaults configurables: `max_steps=8`, `max_tool_calls=6` y estimación de 4.000
tokens por intento. Agotar cualquiera produce `AgentLoopLimitError`; nunca se
inventa un final. Una tool desconocida o args semánticamente inválidos generan
observación JSON de error y consumen paso/tool-call; límites o fallos internos
son errores tipados.

Tools locales, todas offline y de solo lectura:

- `list_sources()`: IDs, URLs, longitudes y hashes, sin contenido.
- `get_source(source_id,start,length)`: slice crudo con máximo configurable
  4.000 caracteres (default solicitado 2.000). La observación contiene
  `source_id`, `start`, `requested_length`, `returned_length`, `next_start`,
  `has_more` y `content`. El límite recorta el campo content, nunca el JSON; no
  normaliza Unicode ni espacios.
- `find(source_id,needle)`: búsqueda literal case-sensitive, sin regex; needle
  máximo 256 caracteres y máximo 20 offsets, con `has_more`. Needle vacío genera
  observación de error, no todas las posiciones.

Cada observación se serializa como JSON válido y se añade a los contents del
mismo rol. Ninguna config enviada al cliente contiene `tools`, grounding o
retrieval.

### Citas y gating fail-closed

El formato V2 offset canónico es `{source_id,start,end,quote}`, con `start`
inclusivo, `end` exclusivo y la invariancia literal
`content[start:end] == quote`. Certificador y fiscal declaran `source_id`; los
offsets explícitos se validan sin búsqueda. Si el modelo omite ambos offsets,
Python los hidrata únicamente cuando `quote` tiene una sola ocurrencia literal
dentro de esa fuente. Cero o múltiples ocurrencias fallan cerrado. Los campos
desconocidos del envelope de cita se conservan/ignoran y no invalidan los cuatro
campos obligatorios del contrato hidratado.

Después del schema del rol se hidratan, extraen y verifican **todas** las citas
contra el snapshot congelado, y se vuelven a verificar inmediatamente antes de
construir el `AgentRunResult`, seam actual de consumo/persistencia. Cualquier
formato, fuente, quote u offset inválido rechaza
el output completo con `AgentOutputRejected`; nunca se ignora una cita mala. Una
decisión afirmativa del certificador (`indice_oficial`, combinado o portal
externo) exige al menos una cita y, fiel a la skill, las dimensiones
`identity`, `page_role`, `bucket` y `stability`. En el fiscal, `block` o cualquier acusación
`proved` exige al menos una cita offset verificable; `sustain`, `needs_tool`,
`review` y resultados negativos pueden tener cero. `verify_all([])` por sí solo
no satisface un resultado afirmativo.

### Fiscal independiente y schema propuesto

No existe schema canónico del fiscal. Ronda 5 crea únicamente el schema V2-local
`Fase2ProsecutorOutputV2ProposalForOrion` en `agents/schemas.py`, marcado
explícitamente como **propuesta para Orion, no promovida**. Deriva fielmente de
la skill: `result`, `reason`, lista de `accusations` con outcome
`proved|discarded|unresolved` y citas, citas globales, `tool_request` y
`failure_mode_proposal`. `block` exige al menos una acusación probada.
Python exige además las 15 acusaciones canónicas, sin duplicados; `sustain` no
puede contener una acusación probada y `needs_tool` exige un tool request.

La tarea fiscal contiene solamente `certifier_output` y `snapshot_sha256`; el
inventario inicial y sus tools operan sobre el mismo snapshot. No se copian
contents, observaciones, llamadas a tools, tarea privada ni mensajes del
certificador. Logging registra rol, número de paso, acción, nombre de tool,
contador y decisión, sin prompts, contents ni citas completas.

## Juez cerrado y orquestación A/B/C (ronda 6)

`ABCOrchestrator.run()` ejecuta A (certificador), entrega su output validado a B
(fiscal) y adapta ambos a `DecisionProposal`. B `sustain` conserva la propuesta
de A; cualquier objeción no resuelta se adapta conservadoramente a `revisar`.
`resolve()` también expone el seam de propuestas ya validadas para pruebas y
futuras fuentes A/B sin cambiar el cierre determinista.

El desacuerdo es exactamente la tupla `(decision, bucket, resource_identity)`.
`resource_identity` reutiliza `_normalized_candidate_url()` de la cadena
existente: host sin `www`, fragmento y slash terminal eliminados, y query
ordenada. Motivos distintos no crean desacuerdo. Dos `revisar` siempre son
acuerdo conservador; el juez no se llama. La misma decisión sobre recursos
materialmente distintos sí lo llama.

`ConflictJudge` recibe exclusivamente un `StructuredGeminiClient` inyectado. Su
schema permite solo `{decision, reason}`, donde decision pertenece a
`aceptar_A|aceptar_B|revisar`; `additionalProperties=false` impide citas o una
decisión nueva. `build_judge_client()` selecciona el modelo mediante
`RoleModels.judge_model` (`gemini-3.5-flash`) y no resuelve credenciales. Snapshot,
candidatos y outputs A/B se serializan entre delimitadores `UNTRUSTED_DATA` con
la instrucción explícita de que son datos, nunca instrucciones.

C solo se invoca cuando las tuplas difieren. Si elige A/B, el orquestador
reconstruye desde esa propuesta; si devuelve `revisar`, no inventa un final. En
todos los casos de consenso se ejecuta igualmente `_final_gate()`: las citas se
validan otra vez con `anchor_citation(..., require_offsets=True)` y `verify_all`,
y el recurso pasa por `resolve_selector_pick()` y `derive_final_decision()` de la
cadena existente. No se reproducen sus comprobaciones de autoridad, identidad,
evidencia, bucket o elegibilidad dentro del juez.

Códigos discretos de revisión: `input_invalid`, `proposal_invalid`,
`agreement_review`, `consensus_failed_final_gate`, `judge_error`,
`judge_ambiguous`, `judge_invalid_citation` y `judge_failed_final_gate`.
Timeout, cancelación, cuota y errores tipados del cliente, además de respuesta
vacía/malformada o serialización inválida, se convierten en `judge_error` en el
adaptador. No existe un `except` general que oculte errores internos.

## Memoria externa append-only sin influencia (ronda 7)

La escritura vive en `v2/memory/store.py`; el pipeline solo conoce el protocolo
local `CaptureSink` write-only de `agents/orchestration.py`. El sink se construye
fuera del pipeline con un `LearningCandidate` ya estructurado. No genera texto,
no consulta modelos, red, entorno, ADC, skills ni memoria previa. Auditoría
(`memory/audit.py`) y promoción (`memory/promotion.py`, `memory/audit_cli.py`)
son módulos separados que `agents/orchestration.py` no importa.

El log runtime es `staging/fase2_v2/memory/learnings.jsonl`. Cada evento schema
1 contiene `id`, `schema_version`, `created_at`, `source_case={municipio,
snapshot_ref}`, `observation`, `proposed_generalization` y `status="staged"`.
`created_at` es un `datetime` timezone-aware inyectado; no se consulta reloj
real. El ID es SHA-256 del JSON UTF-8 canónico de source_case, observation,
generalización y schema_version; el timestamp queda fuera. Capturas idénticas
se anexan como líneas separadas con el mismo ID. La auditoría puede colapsarlas
por ID y reportar `occurrences`, sin deduplicar en escritura.

Los textos no confiables exigen tipo string, se acotan (municipio/actor 200,
snapshot ref 256 y textos 4.000 caracteres) y todo carácter Unicode de control
se representa como secuencia visible `\\uXXXX`. Se serializa JSON estricto con
`allow_nan=false`. Esos valores nunca forman rutas, filenames ni prompts; el
path del store se configura por separado.

`ABCOrchestrator.resolve()` termina primero el gate y materializa una
`FinalDecision`; si hay sink, `_serialize_final_decision()` produce su JSON
canónico antes de `capture()`. Errores concretos de I/O, validación o JSON se
devuelven en `capture_report` como `capture_error`; la `FinalDecision` ya creada
no cambia ni se reemplaza. Tests con spy demuestran que ningún método lector se
invoca durante decisiones con o sin staging estacionado.

Cada append abre un archivo `.lock`, toma `flock` exclusivo y ejecuta un único
`os.write` con una línea JSON completa más newline, seguido de `fsync`. El lector
rechaza cualquier línea completa corrupta, pero ignora una única última línea
sin newline como resto truncado de una caída. Futuras versiones agregan eventos
con otro `schema_version`; nunca migran reescribiendo historia.

Promover no cambia el evento staged. La acción humana explícita agrega un evento
nuevo a `staging/fase2_v2/memory/promotion_events.jsonl` con `learning_id`,
`promoted_at` timezone-aware inyectado, `actor`, `schema_version` y
`event="promoted"`. El único entrypoint es el CLI separado:

```bash
python -m scripts.fase2_municipios.v2.memory.audit_cli audit \
  --learnings staging/fase2_v2/memory/learnings.jsonl
python -m scripts.fase2_municipios.v2.memory.audit_cli promote \
  --learnings staging/fase2_v2/memory/learnings.jsonl \
  --promotions staging/fase2_v2/memory/promotion_events.jsonl \
  --learning-id <sha256> --actor <humano> --promoted-at <ISO-8601-con-zona>
```

Ambos JSONL y sus locks están ignorados por Git. No existe API de promoción en
certificador, fiscal, juez, gate u orquestador.

## Invariantes de seguridad

- Free-only: V2 usa únicamente `GEMINI_API_KEY_FREE` explícita. La presencia de
  `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_API_KEY` o `GEMINI_API_KEY` es una
  credencial no autorizada y aborta antes de construir el SDK. Ante cuota o
  error, espera/falla de forma tipada; nunca degrada a otra credencial.
- El juez no consulta `os.environ`, ADC ni nombres de credenciales: recibe el
  cliente free estructurado ya construido e inyectado.
- Grounding siempre off: ningún payload incorpora Google Search ni herramientas
  externas. Las tools permitidas operan contra evidencia local/congelada.
- La suite V2 instala un guard autouse de sesión que bloquea `connect`,
  `connect_ex` y `create_connection` fuera de loopback/AF_UNIX y contabiliza cada
  intento bloqueado para pruebas offline.
- Structured output obligatorio, seguido de validación estricta contra el schema
  de salida correspondiente; JSON inválido no se reinterpreta heurísticamente.
- Snapshot, candidatas y outputs de agentes son datos no confiables; su contenido
  delimitado no puede introducir instrucciones ni ampliar el dominio del juez.
- Toda cita debe localizarse literalmente en el `EvidenceSnapshot` por campo y
  hash. Una cita no verificable impide confirmar.
- Tool loop acotado por número de rondas, llamadas, bytes y tiempo; allowlist de
  tools, argumentos validados y trazabilidad completa.
- Rate limiter único y compartido: defaults 15 RPM, 250k TPM y RPD configurable;
  cola/espera justa, reconciliación de tokens y cero fallback.
- Logging estructurado con IDs/hashes, nunca secretos, keys, headers sensibles o
  cuerpos crudos que puedan contenerlos; errores de proveedor se redactan.
- Memoria externa versionada: toda lección entra como evento staged append-only;
  promoción es otro evento escrito solo por el CLI humano separado.

## Diferencial vs V1

V1 combina adquisición, adjudicación determinista, selector Gemini, grounding,
fallback free/pago y output en un script principal. V2 separa carga canónica,
captura congelada, cliente free-only sin grounding, rate limiting compartido,
tres roles independientes y memoria versionada. El cambio es paralelo: V2 no
importa ni altera el estado mutable, CLI o salida canónica de V1.

## Outputs de staging separados

Los outputs runtime escriben solo bajo rutas nuevas, por ejemplo:

```text
staging/fase2_v2/runs/<run_id>/snapshots/
staging/fase2_v2/runs/<run_id>/verdicts/
staging/fase2_v2/runs/<run_id>/audit.jsonl
staging/fase2_v2/memory/learnings.jsonl
staging/fase2_v2/memory/promotion_events.jsonl
```

No se escribirá `data/fase2/municipios_rs.csv`, `data/cascade_output.csv`, ningún
CSV canónico ni `authority_first/data/`. La promoción a cualquier consumidor de
producción queda fuera de esta arquitectura de staging y requiere una ronda y
autorización explícitas.

## Runner golden y diferencial V1/V2 reproducible (ronda 8)

El runner vive en `scripts/fase2_municipios/v2/eval/golden_runner.py`. Su unidad
es `(municipio, bucket)`, con los buckets cerrados `concurso_publico` y
`processo_seletivo`: proceden respectivamente de `url_concursos` más
`urls_concursos_extra`, y `url_processos_seletivos` más
`urls_processos_extra` del golden. `indice_oficial_combinado` confirma la unidad
evaluada de ambos buckets; `portal_externo_oficial` confirma solo el bucket que
declara la unidad del corpus.

El artefacto fuente de verdad es JSON schema 1 en UTF-8, claves ordenadas, listas
ordenadas, LF final y sin timestamps, rutas absolutas ni metadata del entorno.
Cada fila contiene golden, decisión/recurso/evidencia V1, decisión/recurso/citas/
evidencia V2 y las tres clasificaciones cerradas. `flip_v1_v2` admite solamente
`both_confirm_same_resource`, `both_confirm_distinct_resource`,
`v2_confirm_v1_review`, `v1_confirm_v2_review`, `both_review`, `both_negative`,
`v2_confirm_v1_negative`, `v1_confirm_v2_negative`,
`v2_review_v1_negative` y `v1_review_v2_negative`. `v1_vs_golden` y
`v2_vs_golden` admiten `match`, `differ` o `golden_na`. El CSV es una vista
derivada para humanos, no la base del determinismo.

`replay` consume un corpus schema 1 congelado por unidad: bloque V1 con decisión
y evidencia propia; bloque V2 con `EvidenceSnapshot`, propuestas A/B y casete de
C. Las citas se vuelven a validar contra el snapshot y el resultado V2 se obtiene
mediante `ABCOrchestrator`. La identidad del recurso importa
`cascade_municipios._normalized_candidate_url`; la paridad golden llama a
`scripts/eval/medir_golden_set.py`, sin copiar métricas. Falta de unidad,
evidencia, fuente, propuesta o casete aborta explícitamente y nunca fabrica
`nao_encontrado`. Los fixtures bajo `eval/tests/fixtures/` son sintéticos y no
son el corpus representativo de 24 municipios.

La hoja `adjudication` incluye únicamente divergencias y mantiene bloques V1 y
V2 separados: snapshot ref, autoridad, identidad, motivo y, solo para V2,
fuentes y citas ancladas. Un flip o diferencia contra golden se informa y nunca
es gate automático. La confirmación sigue sujeta al gate determinista V2; la
adjudicación humana conserva la política de cero falsos positivos.

`live` exige adaptadores inyectados de fetch/modelo/reloj. Antes de delegar una
request, valida proveedor `gemini_free`, los tres modelos de `RoleModels`,
credencial free explícita, ausencia de `GEMINI_API_KEY_PAID`, `GEMINI_API_KEY` y
`GOOGLE_APPLICATION_CREDENTIALS`, y grounding desactivado mediante el knob real
del SDK/config: `tools` debe estar ausente o ser `None`. El CLI permite validar
el contrato offline con:

```bash
GEMINI_API_KEY_FREE=<free-key> python -m \
  scripts.fase2_municipios.v2.eval.golden_runner live \
  --provider gemini_free --tools none --validate-only
```

Para la ejecución real Orion inyecta sus adaptadores en `run_live`; este módulo
no agrega fallback pago ni ADC. V1 no ofrece hoy un seam live limpio que elimine
su fallback histórico, por lo que el replay lo envuelve externamente y no cambia
su lógica. Los artefactos de corrida van en `staging/fase2_v2/eval/`, ignorado
por Git. El golden, CSV canónico, V1 y skills permanecen intactos.

## Selector estratificado para staging live (ronda 9)

El selector offline vive en
`scripts/fase2_municipios/v2/eval/stratified_selector.py`; toma como universo
`authority_first/data/municipios_resources_rs.csv` y excluye por defecto las 24
identidades de `golden_set_v1.csv`. La exclusión no compara texto crudo: crea
identidades municipales con `cascade_municipios.norm` e identidades de recurso
con `cascade_municipios._normalized_candidate_url`. Coincidir por cualquiera de
las dos excluye la fila.

La precedencia de familia es única, cerrada y está versionada en
`portal_families_v1.json`: `multi24`, `oxy_elotech`, `atende_net`,
`govbr_cloud`, `rs_gov_br` y fallback `desconocida`. Los cuatro primeros están
declarados difíciles. Las reglas usan únicamente sufijos de host o fragmentos
generales de URL; no contienen municipios, observaciones libres ni portales de
casos individuales. `ip_delegado`, `multiples_hosts` y
`usa_transparencia_externa` son booleanos separados y nunca crean familias
adicionales.

Los valores V1 observados `boa`, `nao_encontrada` y `revisar` se conservan en
`estado_fuente`. Su agregado pertenece al vocabulario cerrado `confirmado`,
`nao_encontrado`, `revisar`, `misto` o `sem_saida_previa`; este último significa
ausencia de salida y no se confunde con un negativo. Un valor fuente desconocido
aborta. `borderline` se deriva de la lista ordenada de razones
`v1_revisar`, `familia_dificil`, `senal_ambigua`.

La selección es jerárquica y no asigna puntos: primero reserva diez borderlines,
round-robin entre familias lexicográficas y estados lexicográficos dentro de la
familia; después llena el resto round-robin por familia, manteniendo estado como
cobertura secundaria. Un estrato escaso se agota y el turno sobrante pasa al
siguiente estrato de forma determinista. Los candidatos se ordenan por identidad
antes de que `random.Random(seed)` baraje exclusivamente dentro de cada estrato
`(familia, estado)`; el RNG nunca ordena estratos.

El JSON schema 1 canónico es la fuente reproducible: UTF-8, LF, claves ordenadas,
sin timestamps ni rutas ambientales. Incluye seed, tamaños, orden de estratos,
muestra etiquetada y cobertura por familia, estado, borderline, señal y fase. El
CSV es solo una vista derivada. Comando para preparar los 50 casos de Orion:

```bash
python -m scripts.fase2_municipios.v2.eval.stratified_selector \
  --universe authority_first/data/municipios_resources_rs.csv \
  --golden authority_first/data/golden_set_v1.csv \
  --output-json staging/fase2_v2/eval/selector/sample.json \
  --output-csv staging/fase2_v2/eval/selector/sample.csv \
  --size 50 --seed <seed-acordada> --borderline-minimum 10
```

`staging/fase2_v2/eval/` ya está ignorado por Git. El selector no importa red,
cliente Gemini ni credenciales y no modifica V1, skills, golden o CSV canónico.

## Decisiones abiertas para el CEREBRO

- Revisar periódicamente la disponibilidad free de los modelos fijados en ronda
  3 (`gemini-3.1-flash-lite` para certificador/fiscal y `gemini-3.5-flash` para
  juez); el cliente nunca cambia de modelo ni cae a pago automáticamente.
- Definir el RPD free exacto por modelo/proyecto y su política de rollover; RPM y
  TPM quedan fijados como defaults configurables, no como garantía del proveedor.
- Especificar schemas separados para fiscal y juez: el schema canónico actual
  cubre solo la salida del certificador.
- Fijar máximos del tool loop y estrategia exacta de conteo/reconciliación de
  tokens después de medir prompts reales.
- Definir retención y autoridad organizacional de promoción; formato, versión y
  frontera humana ya están fijados por los eventos append-only de ronda 7.
- Decidir si `EvidenceSnapshot` conserva HTML completo fuera del objeto principal
  mediante content-addressed storage y cuáles son las reglas de redacción.
- Resolver la divergencia Python local 3.14.4 versus objetivo 3.12 antes de una
  implementación de producción.
- Aportar el corpus replay real de los 24 municipios; los fixtures sintéticos
  solo prueban el contrato y no constituyen una medición representativa.
- Revisar la tabla de familias cuando aparezcan proveedores nuevos: el CSV
  previo actual no contiene `multi24`, `oxy_elotech` ni `govbr_cloud`, aunque el
  golden sí documenta los dos primeros.

Decisiones triviales resueltas conservadoramente en este paso: UTF-8 explícito,
orden lexicográfico/fijo, contratos mínimos que no exigen opcionales, búsqueda de
raíz acotada a ocho padres, errores sin volcado de contenido y estructuras
recursivamente inmutables.
