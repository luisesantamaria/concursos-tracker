# Triage de la cola de riesgo — Fase 2

Fecha de inspección: 2026-07-10. Alcance: corpus congelado y artefactos locales; sin red, scraping, cascade, Playwright ni chunks 5/6.

## Forense de los 8

Los ocho antecedentes se localizaron por `municipio` exacto en el corpus. La tabla muestra **todos** los JSON exactos de esos municipios (no los homónimos; por ejemplo, se excluye Estrela Velha). El veredicto histórico aplica al bucket auditado indicado en la última columna; los demás JSON sólo documentan el contraste forense.

| municipio | slug/archivo | bucket | decision JSON | url JSON | antecedente auditado |
|---|---|---|---|---|---|
| Itaara | itaara_concursos.json | concursos | confirmar | https://itaara.rs.gov.br/concurso | FP:noticia-numeros-basura (FP claro) |
| Itaara | itaara_processos.json | processos | revisar | https://itaara.rs.gov.br/portal-da-transparencia/contratacoes-emergenciais | — |
| Canudos Do Vale | canudos_do_vale_concursos.json | concursos | confirmar | https://www.canudosdovale.rs.gov.br/concursos-publicos | FP:menu-sin-listado (FP probable) |
| Canudos Do Vale | canudos_do_vale_processos.json | processos | confirmar | https://www.canudosdovale.rs.gov.br/processo-seletivo-simplificado | — |
| Estrela | estrela_concursos.json | concursos | confirmar | https://estrela.atende.net/cidadao/pagina/concursos-publicos-e-processos-seletivos | FP:atende-overcount-edital (sobre-conteo probable) |
| Estrela | estrela_processos.json | processos | confirmar | https://estrela.atende.net/cidadao/pagina/concursos-publicos-e-processos-seletivos | — |
| Nova Boa Vista | nova_boa_vista_concursos.json | concursos | confirmar | https://novaboavista.rs.gov.br/pt_BR/concursos | legitimo |
| Nova Boa Vista | nova_boa_vista_processos.json | processos | confirmar | https://novaboavista.rs.gov.br/pt_BR/concursos | — |
| Panambi | panambi_concursos.json | concursos | confirmar | https://panambi.atende.net/cidadao/pagina/concurso | legitimo (lean-legítimo) |
| Panambi | panambi_processos.json | processos | confirmar | https://panambi.atende.net/cidadao/pagina/concurso | — |
| Capão Do Leão | capao_do_leao_concursos.json | concursos | confirmar | https://www.capaodoleao.rs.gov.br/portal/editais/1/3/0/0/0/41/0/0/0/data-realizacao-decrescente/0 | legitimo (lean-legítimo) |
| Capão Do Leão | capao_do_leao_processos.json | processos | confirmar | https://www.capaodoleao.rs.gov.br/portal/editais/1/3/0/0/0/42/0/0/0/data-realizacao-decrescente/0 | — |
| Eldorado Do Sul | eldorado_do_sul_concursos.json | concursos | confirmar | https://www.eldorado.rs.gov.br/portal/editais/3 | legitimo (lean-legítimo) |
| Eldorado Do Sul | eldorado_do_sul_processos.json | processos | confirmar | https://www.eldorado.rs.gov.br/portal/editais/3 | — |
| Condor | condor_concursos.json | concursos | revisar | https://www.condor.rs.gov.br/portal-da-transparencia/concursos-publicos | — |
| Condor | condor_processos.json | processos | confirmar | https://www.condor.rs.gov.br/prefeitura/concursos-2/ | legitimo |

### Búsqueda de manifiesto

No apareció un CSV/Markdown con una lista cerrada de los ~30. La búsqueda read-only en el repositorio, `/home/orion`, los logs y el historial de esta ruta no encontró un manifiesto versionado. Sí apareció la fuente histórica que explica la selección: `/home/orion/.hermes/state.db`, tabla `messages`, filas 222 y 284 (duplicadas también en `/home/orion/.hermes/profiles/atlas/state.db`, fila 63). Su contenido dice: «El tail… (~30), y son 3 firmas concretas», y enumera `piso0+ih0`, páginas cortas `<1600 chars` y `io >> ih`; además registra los ocho veredictos usados arriba. Los CSV que contienen los ocho son listados completos de municipios, no manifiestos de cola.

## Definición de la cola canónica CORREGIDA

La hipótesis `decision=revisar` queda refutada: el cruce del corpus es 197 `revisar/concursos`, 92 `revisar/processos`, 119 `confirmar/concursos` y 210 `confirmar/processos`; de los ocho antecedentes, sólo `itaara_processos.json` y `condor_concursos.json` son `revisar`, mientras los ocho buckets auditados son `confirmar`.

La cola canónica **C** es la unión, sobre JSON `decision=confirmar`, de las tres firmas históricas: (1) `piso=0` e `item_here=0`; (2) texto capturado `<1600` caracteres; (3) para `concursos`, contenido del tipo opuesto fuertemente dominante, operacionalizado de forma transparente como `item_other >= 4 × max(1,item_here)`. La tercera regla es la traducción verificable de `io >> ih` de la fuente histórica; no es un scorer ni se implementó en código. Produce **31 buckets**, que coincide con el «~30» registrado, y contiene los ocho antecedentes exactos: Itaara C, Canudos do Vale C, Estrela C, Nova Boa Vista C, Panambi C, Capão do Leão C, Eldorado do Sul C y Condor P.

B (`audit_progress` no-OK) era erróneo para esta finalidad: sólo contenía Itaara/procesos, no los otros siete, y 43 de sus filas no tenían JSON. No se reutiliza ninguna de sus conclusiones. C tiene JSON para 31/31 buckets; tras excluir los ocho ya cerrados, quedan **23** y todos tienen evidencia congelada (0% sin JSON).

### Manifiesto reproducible de los 31 buckets

Fuente: `/home/orion/.hermes/state.db`, tabla `messages`, filas 222 y 284, más la unión reproducible de las tres firmas allí descritas sobre `/home/orion/.hermes/run497_corpus`: `decision=confirmar` y (`piso=0 & item_here=0` o texto capturado `<1600` o `item_other >= 4×max(1,item_here)` para concursos). Estas firmas se usan exclusivamente para reconstruir la cola histórica; **no son constantes ni reglas del runtime**.

| ID canónico | etiqueta esperada |
|---|---|
| `arroio_do_tigre_concursos` | `FP:detalle-individual-documentos` |
| `aurea_concursos` | `legitimo` |
| `boqueirao_do_leao_concursos` | `legitimo` |
| `cangucu_concursos` | `legitimo` |
| `canoas_processos` | `FP:noticia-numeros-basura` |
| `canudos_do_vale_concursos` | `FP:menu-sin-listado` |
| `canudos_do_vale_processos` | `legitimo` |
| `capao_do_leao_concursos` | `legitimo` |
| `chiapetta_concursos` | `legitimo` |
| `condor_processos` | `legitimo` |
| `eldorado_do_sul_concursos` | `legitimo` |
| `estrela_concursos` | `FP:atende-overcount-edital` |
| `flores_da_cunha_processos` | `legitimo` |
| `frederico_westphalen_concursos` | `legitimo` |
| `horizontina_concursos` | `FP:menu-sin-listado` |
| `imbe_concursos` | `FP:menu-sin-listado` |
| `imbe_processos` | `FP:menu-sin-listado` |
| `itaara_concursos` | `FP:noticia-numeros-basura` |
| `itapuca_concursos` | `legitimo` |
| `itapuca_processos` | `legitimo` |
| `lajeado_do_bugre_processos` | `FP:menu-sin-listado` |
| `nova_boa_vista_concursos` | `legitimo` |
| `nova_padua_processos` | `legitimo` |
| `nova_petropolis_concursos` | `FP:detalle-individual-documentos` |
| `nova_ramada_processos` | `legitimo` |
| `novo_xingu_concursos` | `legitimo` |
| `panambi_concursos` | `legitimo` |
| `pinheirinho_do_vale_processos` | `legitimo` |
| `pinto_bandeira_concursos` | `FP:menu-sin-listado` |
| `poco_das_antas_concursos` | `revisar_humano` |
| `presidente_lucena_concursos` | `legitimo` |

## Matriz de paridad de señales

El cierre real consume `title`, texto visible normalizado, anclas `(texto, href)`, `items_llm`, bucket y municipio. Antes de llamar al adjudicador, el runtime también conoce URL, estado HTTP, error de descarga y señales SPA/antibot. El JSON congelado persiste URL, municipio, bucket, título, texto, anclas, `items_llm`, decisión y evidencia capturadas; no persiste el DOM ni el estado HTTP original.

| señal requerida | runtime | corpus run497 | uso |
|---|---|---|---|
| Título y texto visible | sí | sí | encabezado gobernante, cuerpo editorial, errores visibles y filas de listado |
| Texto/href de anclas | sí | sí | distinguir entradas navegables, PDF/anexos y chrome de menú |
| Ítems estructurados del extractor | sí | sí (`items_llm`) | apoyo a entradas de evento, nunca autoridad única |
| Bucket y municipio | sí | sí | compatibilidad de la entrada con concurso/PSS |
| URL | sí | sí | trazabilidad; no se usan proveedores, IP ni slugs como regla |
| HTTP/error/SPA/antibot | sí, antes del adjudicador | no | `content_complete` es más fuerte online; esta parte no es validable offline y requiere corrida local |
| DOM (`article`, `time`, H1/H2, cards/filas), JSON-LD | no llega al adjudicador | no | no se usa; no puede fundamentar un predicado actual |

Por esta paridad, los cuatro predicados se limitan a título, texto, anclas, ítems y señales de recuperación visibles. La distinción de contenido incompleto basada en HTTP/SPA queda marcada como **no validable offline → requiere corrida local**; en el corpus sólo puede comprobarse el contenido efectivamente persistido.

## Tabla de triage completa

`decision_pipeline` reproduce la confianza del CSV de Fase 2; el CSV no guarda un código de decisión discreto. La URL se toma del CSV y la cita de la captura congelada.

| municipio | bucket | url_pipeline | decision_pipeline | veredicto | evidencia (archivo + fragmento) | nota |
|---|---|---|---|---|---|---|
| Arroio Do Tigre | concursos | http://www.arroiodotigre.rs.gov.br/site/concurso-publico-2025 | confirmado | FP:detalle-individual-documentos | `arroio_do_tigre_concursos.json`: «Concurso Público 2025… DOWNLOADS DE DOCUMENTOS… EDITAL DE CONVOCAÇÃO nº 31/2026».  | Un certamen individual con documentos/convocatorias, no índice multi-evento. |
| Áurea | concursos | https://aurea.rs.gov.br/download-category/concurso-publico/ | confirmado | legitimo | `aurea_concursos.json`: «n_certames=2; certames=[['1', '2014'], ['1', '2016']]».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Boqueirão Do Leão | concursos | https://www.boqueiraodoleao.rs.gov.br/php/leis.php?t=2 | confirmado | legitimo | `boqueirao_do_leao_concursos.json`: «Concurso Publico 001/2024».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Canguçu | concursos | https://www.cangucu.rs.gov.br/portal/editais/3 | confirmado | legitimo | `cangucu_concursos.json`: «n_certames=28; certames=[['113', '2026'], ['114', '2026'], ['115', '2026']]».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Canoas | processos | — | — | FP:noticia-numeros-basura | `canoas_processos.json`: título «Processos seletivos para a saúde de Canoas contam com 8.305 inscritos – Prefeitura Municipal de Canoas»; texto «Notícias 16/01/2022 Saúde… 8.305 inscritos».  | Artículo único; los números no prueban varios certámenes. |
| Canudos Do Vale | processos | https://www.canudosdovale.rs.gov.br/processo-seletivo-simplificado | confirmado | legitimo | `canudos_do_vale_processos.json`: «Processo Seletivo Simplificado».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Chiapetta | concursos | https://chiapetta.rs.gov.br/prefeitura/concursos-e-processos-seletivos/ | confirmado | legitimo | `chiapetta_concursos.json`: «Concurso Público 01/2023».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Flores Da Cunha | processos | https://pmfloresdacunha.multi24h.com.br/multi24/sistemas/transparencia/?entidade=1&secao=dinamico&id=13600 | confirmado | legitimo | `flores_da_cunha_processos.json`: «PSS 04/2025».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Frederico Westphalen | concursos | https://www.fredericowestphalen-rs.com.br/contratacao/index?ContratacaoSearch%5Bid_contratacao_categoria%5D=1&ContratacaoSearch%5Bano%5D=&ContratacaoSearch%5Bq%5D= | confirmado | legitimo | `frederico_westphalen_concursos.json`: «CONCURSO PÚBLICO N° 001/2020».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Horizontina | concursos | https://horizontina.atende.net/cidadao/pagina/concursos-publicos | confirmado | FP:menu-sin-listado | `horizontina_concursos.json`: «Confira aba Páginas para mais links… Concursos Públicos 2021 2023 2025». | Navegación/atajos sin listado directo de eventos del bucket. |
| Imbé | concursos | http://www.imbe.rs.gov.br/conteudo/13400/10942?titulo=CONCURSOS | confirmado | FP:menu-sin-listado | `imbe_concursos.json`: «Concursos 2023; Concursos Anteriores a 2023; Concurso 2025» (enlaces de menú). | Navegación/atajos sin listado directo de eventos del bucket. |
| Imbé | processos | https://www.imbe.rs.gov.br/processos-seletivos | confirmado | FP:menu-sin-listado | `imbe_processos.json`: «Concursos 2023; Concursos Anteriores a 2023; Concurso 2025» (el contenido sigue en Concursos). | Navegación/atajos sin listado directo de eventos del bucket. |
| Itapuca | concursos | https://site.itapuca.rs.gov.br/concurso/ | confirmado | legitimo | `itapuca_concursos.json`: «CONCURSO PÚBLICO 01/2018».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Itapuca | processos | https://site.itapuca.rs.gov.br/processoseletivo/ | confirmado | legitimo | `itapuca_processos.json`: «PROCESSO SELETIVO SIMPLIFICADO Nº 003/2026 – Psicólogo».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Lajeado Do Bugre | processos | https://lajeadodobugre.rs.gov.br/prefeitura/concursos-e-processos-seletivos/ | confirmado | FP:menu-sin-listado | `lajeado_do_bugre_processos.json`: «Concursos e Processos Seletivos 2026… 2015» (sólo enlaces por año). | Navegación/atajos sin listado directo de eventos del bucket. |
| Nova Pádua | processos | https://www.novapadua.rs.gov.br/portal.php?pagina=portal_concursos&modalidade=2 | confirmado | legitimo | `nova_padua_processos.json`: «PROCESSO SELETIVO SIMPLIFICADO Nº 004/26 - PROF. DE ED. INFANTIL / ENS. FUNDAMENTAL, PROF. DE MATEMÁTICA, PROF. DE LÍNGUA PORTUGUESA E AUX. DE SERVIÇO».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Nova Petrópolis | concursos | https://www.novapetropolis.rs.gov.br/portal-da-transparencia/concurso-publico | confirmado | FP:detalle-individual-documentos | `nova_petropolis_concursos.json`: anclas «Edital 17 2023 - Notas Preliminares…», «Edital 18 2023…» del mismo concurso. | Un certamen individual con documentos/convocatorias, no índice multi-evento. |
| Nova Ramada | processos | https://www.novaramada.rs.gov.br/editais | confirmado | legitimo | `nova_ramada_processos.json`: «Processo Seletivo 03 - 2026».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Novo Xingu | concursos | https://www.novoxingu.rs.gov.br/transparencia/adm/concursos | confirmado | legitimo | `novo_xingu_concursos.json`: «Concurso Público nº 001/2023».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Pinheirinho Do Vale | processos | https://pinheirinhodovale.rs.gov.br/processos-seletivos/ | confirmado | legitimo | `pinheirinho_do_vale_processos.json`: «Processo Seletivo N° 01-2026. Cargos Educador Físico, Assistente Social, Psicólogo, Monitor, Servente, Zelador e Motorista.».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |
| Pinto Bandeira | concursos | — | — | FP:menu-sin-listado | `pinto_bandeira_concursos.json`: «ACESSO RÁPIDO… Concurso 001/2019… Edital do Concurso Público 2023». | Navegación/atajos sin listado directo de eventos del bucket. |
| Poço Das Antas | concursos | https://www.pocodasantas.rs.gov.br/portal-da-transparencia/concursos-publicos | confirmado | revisar_humano | `poco_das_antas_concursos.json`: «CONCURSO PÚBLICO 2026… Até o dia 21/05/2026 não foi realizado Concurso Público»; luego sólo «PROCESSO SELETIVO PÚBLICO Nº 01/2026».  | La categoría concursos muestra que no hubo concurso y sólo expone un PSS; no hay segundo caso idéntico para crear familia. |
| Presidente Lucena | concursos | https://www.presidentelucena.rs.gov.br/arquivos/concursos-publicos | confirmado | legitimo | `presidente_lucena_concursos.json`: «CONCURSO PÚBLICO Nº01/2026».  | Categoría/portal con múltiples eventos del bucket (o índice combinado que incluye el tipo). |

## Familias de FP confirmadas

| Familia | Mecanismo | Municipios/buckets afectados | Qué tendría que cambiar el código |
|---|---|---|---|
| `noticia-numeros-basura` | Noticia individual cuyos números se interpretan como certámenes. | Itaara (C, antecedente); Canoas (processos). | Rechazar forma noticia antes de contar números o admitirla como índice. |
| `menu-sin-listado` (renombra `menu-por-anio`) | Menú, años o accesos rápidos que enlazan a páginas, pero no listan directamente eventos del bucket. | Canudos do Vale (C, antecedente); Horizontina (C); Imbé (C y processos); Lajeado do Bugre (processos); Pinto Bandeira (C). | Exigir ítems de certamen visibles en la página, no sólo enlaces de navegación/categoría. |
| `atende-overcount-edital` | Documentos/números de edital del mismo proceso inflan la identidad de certámenes. | Estrela (C, antecedente); el segundo caso no está demostrado por los 23 pendientes. | Mantener la familia como antecedente, sin ampliarla con evidencia insuficiente. |
| `detalle-individual-documentos` | Una página de un único concurso acumula convocatorias o anexos y se confunde con índice. | Arroio do Tigre (C); Nova Petrópolis (C). | Distinguir el detalle de un solo proceso de una categoría que agrega procesos distintos. |

`atende-overcount-edital` se conserva porque es un antecedente explícito, pero no se cuenta como familia confirmada por esta extensión: no hay segundo municipio nuevo probado dentro de C.

## No triable offline

Ninguno. Los 23/23 buckets pendientes de C tienen JSON con texto y evidencia; porcentaje sin JSON: **0%**.

## Conteos

- Cola canónica C: **31** buckets.
- Antecedentes ya cerrados: **8** buckets.
- Triados en esta extensión: **23** buckets.
- `legitimo`: **14**.
- `FP:noticia-numeros-basura`: **1** (más Itaara C como antecedente).
- `FP:menu-sin-listado`: **5** (más Canudos do Vale C como antecedente).
- `FP:detalle-individual-documentos`: **2**.
- `no_triable_offline`: **0**.
- `revisar_humano`: **1**.
- Comprobación de pendientes: 14 + 1 + 5 + 2 + 0 + 1 = **23**; más 8 antecedentes = **31** buckets de C.

## Contrato estructural implementado

El punto real de cierre es `scripts/eval/cierre_dataset.py::_extract_verdict_from_fixture` / `extract_verdict`, que delega la decisión falsificable en `scripts/eval/verdict_extract.py::adjudicate`. Los cuatro predicados y la tabla de estados se implementaron en este último módulo; `cascade_municipios.py` no necesitó cambios porque sus selecciones todavía pasan por este cierre antes de sellar el dataset.

### Qué cuenta como entrada de evento

Cuenta una fila, card o título visible de un concurso/PSS que identifica el certamen por número/año, objeto/cargo, edital de apertura dentro de una categoría declarada o una `Modalidade` ligada a la fila. Una sola entrada basta. En una página cuyo título declara exclusivamente el bucket opuesto, una sola liga puede ser navegación a una hermana; allí se exige estructura repetida para concluir que el contenido realmente enumera el bucket, y ante una sola liga se revisa.

No cuentan: PDF/anexo/documento de ciclo suelto; convocación, homologación, resultado o retificación como si fueran certámenes nuevos; chrome/menu/acceso rápido; filtro o liga por año; definición editorial; licitación, pregón, llamamiento o credenciamiento; concurso cultural. Un edital de apertura sí puede representar la entrada cuando está dentro de una lista/categoría y está ligado al bucket; el mero PDF no.

| predicado | señal estructural y certeza |
|---|---|
| `has_event_listing` | Al menos una entrada visible válida o card con `Modalidade`; excluye explícitamente documentos accesorios, menú/año, publicación no laboral y cultural. No suma puntos. |
| `is_single_article` | Chrome editorial concluyente (`Compartilhe` más fecha/hora y `Veja também`, relacionadas o crédito de noticia), título/cuerpo de noticia y ausencia de shell de listado real. |
| `is_single_event_document_detail` | Exactamente un encabezado gobernante de certamen (colapsando año y número del mismo padre) y una sección/hijos documentales como `VER ANEXOS`, `DOWNLOADS DE DOCUMENTOS` o tabla `Atividade/Data/Edital`. No se decide por cantidad de PDF. |
| `content_complete` | Texto estructurado recuperado y sin challenge/error visible. El cierre online ya separa vacío, WAF, soft-404 y error de servidor antes del adjudicador. Estado HTTP/SPA no persistido sigue sin ser validable en run497. |

### Tabla de estados

| combinación concluyente | estado discreto |
|---|---|
| `is_single_article` | `detalle_individual_rechazado` |
| `is_single_event_document_detail` | `detalle_individual_rechazado` |
| `has_event_listing` y no artículo/detalle | `indice_oficial` |
| no listado y `content_complete` | `nao_encontrado` (el cierre conserva confianza `revisar`) |
| no listado y contenido incompleto | `revisar` |
| cualquier combinación no concluyente | `revisar` |

No se añadió scorer, suma ponderada, URL de proveedor, IP ni slug municipal.

## Validación offline por el camino real

El ANTES quedó congelado en `authority_first/docs/_scratch_antes_31.md`, con HEAD `180289cd9d84c1bcc83bbc61ca8e86d9a6e99e1d`, comando reproducible y salida completa previa a editar. El DESPUÉS usa el mismo helper real de cierre y los mismos `items_llm` capturados.

| ID | etiqueta esperada | ANTES | DESPUÉS | resultado |
|---|---|---|---|---|
| `arroio_do_tigre_concursos` | `FP:detalle-individual-documentos` | revisar: `certame_unico` | revisar: `detalle_individual_rechazado` | rechazo ahora explícito |
| `aurea_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `boqueirao_do_leao_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `cangucu_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `canoas_processos` | `FP:noticia-numeros-basura` | revisar: noticia | revisar: `detalle_individual_rechazado` | rechazo explícito |
| `canudos_do_vale_concursos` | `FP:menu-sin-listado` | confirmado | revisar: `nao_encontrado` | FP cerrado |
| `canudos_do_vale_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `capao_do_leao_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `chiapetta_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `condor_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `eldorado_do_sul_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `estrela_concursos` | `FP:atende-overcount-edital` | confirmado | confirmado: `indice_oficial` | fuera de las 3 familias de esta directiva |
| `flores_da_cunha_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `frederico_westphalen_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `horizontina_concursos` | `FP:menu-sin-listado` | confirmado | revisar: `nao_encontrado` | FP cerrado |
| `imbe_concursos` | `FP:menu-sin-listado` | confirmado | revisar: `nao_encontrado` | FP cerrado |
| `imbe_processos` | `FP:menu-sin-listado` | confirmado | revisar: `nao_encontrado` | FP cerrado |
| `itaara_concursos` | `FP:noticia-numeros-basura` | confirmado | revisar: `detalle_individual_rechazado` | FP cerrado |
| `itapuca_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `itapuca_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `lajeado_do_bugre_processos` | `FP:menu-sin-listado` | confirmado | revisar: `nao_encontrado` | FP cerrado |
| `nova_boa_vista_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `nova_padua_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `nova_petropolis_concursos` | `FP:detalle-individual-documentos` | revisar: `certame_unico` | revisar: `detalle_individual_rechazado` | rechazo ahora explícito |
| `nova_ramada_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `novo_xingu_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `panambi_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `pinheirinho_do_vale_processos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |
| `pinto_bandeira_concursos` | `FP:menu-sin-listado` | revisar: `certame_unico` | revisar: `nao_encontrado` | rechazo ahora explícito |
| `poco_das_antas_concursos` | `revisar_humano` | confirmado | revisar: `nao_encontrado` | ambigüedad conservada honestamente |
| `presidente_lucena_concursos` | legítimo | confirmado | confirmado: `indice_oficial` | sin cambio |

Resultado: **19/19 índices legítimos sin regresión**; 10/10 instancias de las tres familias quedan en rechazo/revisión estructurada; el único ambiguo queda en revisión. Estrela no se presenta como corregido: su familia histórica no fue parte del objetivo de implementación.

Como comprobación adicional, los 31 se pasaron por `cierre_dataset._verdict_from_content(..., extract_mode="authority", fixture_items=...)`, parcheando únicamente la presencia de la API key con un valor offline para alcanzar la rama de fixture. No hubo llamada externa y la tabla resultó idéntica.

### Golden particionado

El golden tiene **24 municipios**. El comando independiente solicitado, ejecutado sobre el CSV existente sin modificarlo, fue:

```bash
.venv/bin/python scripts/eval/medir_golden_set.py \
  --golden authority_first/data/golden_set_v1.csv \
  --pipeline data/fase2/municipios_rs_local.csv --detalle
```

Ese medidor reportó 24/24 municipios cruzados; para los 19 automatizables, precisión estricta combinada 77,1%, tolerante por host 100%, sin `WRNG/F-POS`. Es una medición del CSV congelado, no una supuesta corrida nueva.

Hay evidencia run497 para **18/24 municipios** (36 buckets). Dentro de esa intersección, 14 capturas coinciden exactamente con una URL golden y las 14 conservaron `confirmar`: Almirante Tamandaré do Sul (C/P), Anta Gorda (P), Aratiba (C/P), Bagé (C/P), Bento Gonçalves (C/P), Canoas (C), Passo Fundo (P), Novo Hamburgo (C/P) y Caxias do Sul (C). También se preservó el índice alternativo legítimo Itaqui/P después de reconocer filas prefijadas `NN/AAAA`; Itaara/C se degradó intencionalmente porque su captura es la noticia FP y **no** la URL combinada del golden. No se observó ninguna caída de una captura legítima en los 18 municipios con evidencia.

Los **6/24 sin ningún JSON run497** requieren corrida local; no se afirma cero regresiones sobre sus páginas:

| municipio sin evidencia | posible interacción con predicados |
|---|---|
| Araricá | Categorías en portal delegado: debe verificarse que el render exponga filas y no sólo el contenedor. |
| André da Rocha | `ano=0` debe renderizar eventos; si sólo aparecen filtros/años, caerá conservadoramente en revisión. |
| Santa Maria | Categorías simples: verificar que las entradas visibles lleguen al texto del adjudicador. |
| Viamão | Categorías separadas: mismo riesgo de paridad de render/listado. |
| São Leopoldo | Portal externo con hash y ausencia conocida de PSS: HTTP/SPA y contenido completo sólo se resuelven en corrida local. |
| São Pedro do Sul | Página combinada Atende: verificar render de cards/filas y que no quede como cascarón de navegación. |

La suite offline completa terminó con **38 passed**. Los fixtures de Anta Gorda/C y Nova Boa Vista/C tenían expectativas antiguas `FP/revisar` incompatibles, respectivamente, con el golden y con el manifiesto cerrado de 31; sólo se corrigieron sus campos `label/expected`, conservando la captura y la decisión histórica dentro del JSON.

## Barrido de regresión — corpus run497 completo

Se materializó el adjudicador baseline sin modificar Git:

```bash
git show HEAD:scripts/eval/verdict_extract.py > /tmp/verdict_extract_baseline.py
```

Los **618 JSON** se ejecutaron dos veces por `cierre_dataset._verdict_from_content(..., extract_mode="authority", fixture_items=...)`: una con el módulo baseline importado desde `/tmp` y otra con el working tree. La presencia de API key se parcheó con un valor offline únicamente para alcanzar la rama de fixture; no hubo llamada externa.

El primer barrido produjo 65 flips, incluidos 53 `confirmado→revisar`: bandera roja. La causa era que `has_event_listing` intentaba reinterpretar formatos que el adjudicador ya había extraído correctamente —filas con fecha prefijada, tablas `Nº/ANO`, cards de `Modalidade` y títulos de edital—. Se ajustó de forma booleana, sin scorer:

- La presencia de certámenes ya verificados por el adjudicador se reutiliza como evidencia positiva secundaria sólo cuando hay eventos repetidos.
- Un solo evento sigue bastando cuando existe una fila/card/título visible concluyente.
- El fallback no vence a una forma concluyente de noticia, detalle, menú/años, título del bucket opuesto o declaración expresa de ausencia.
- Una página con varias filas independientes `VER ANEXOS` no se clasifica como detalle individual; esto corrigió la regresión observada en Campina das Missões/P.

El barrido final produjo **22/618 flips (3,56%)**: 12 correcciones de falsos negativos hacia índice, 9 rechazos de FP y 1 revisión operativa conservadora. **REGRESIONES de índice legítimo: 0**.

| bucket | ANTES→DESPUÉS | clasificación | evidencia congelada |
|---|---|---|---|
| `arambare_processos` | revisar→confirmado | MEJORA (FN) | `arambare_processos.json`: «Categoria: Processo Seletivo», «Processo Seletivo Público n 01/2023» y varias entradas. |
| `capivari_do_sul_concursos` | revisar→confirmado | MEJORA (FN) | `capivari_do_sul_concursos.json`: tabla «Miniatura Nome Data» con entrada «Concurso-01-2023». |
| `catuipe_concursos` | revisar→confirmado | MEJORA (FN) | `catuipe_concursos.json`: «Nº/ANO MODALIDADE OBJETO…» y filas `Concurso` ligadas al CP 01/2017. |
| `cerro_grande_concursos` | revisar→confirmado | MEJORA (FN) | `cerro_grande_concursos.json`: sección genérica «CONCURSO PÚBLICO / 2025» y entrada «Concurso Público 001/2025». |
| `charqueadas_processos` | revisar→confirmado | MEJORA (FN) | `charqueadas_processos.json`: varias entradas independientes de selección pública simplificada. |
| `doutor_mauricio_cardoso_processos` | revisar→confirmado | MEJORA (FN) | `doutor_mauricio_cardoso_processos.json`: lista de PSS separados para estagiarios, médico veterinario, operario, ACS y otros. |
| `encantado_concursos` | revisar→confirmado | MEJORA (FN) | `encantado_concursos.json`: card «CONCURSO PUBLICO 01/2025 - ABERTURA DAS INSCRIÇÕES», `Modalidade: Concurso`; los mensajes «não houve» sólo separan intervalos sin publicaciones. |
| `erval_grande_concursos` | revisar→confirmado | MEJORA (FN) | `erval_grande_concursos.json`: página genérica «CONCURSO PÚBLICO» con el CP 02/2025 y su ciclo documental completo. |
| `inhacora_processos` | revisar→confirmado | MEJORA (FN) | `inhacora_processos.json`: página combinada y entrada «Processo Seletivo Simplificado Contratação de Professores». |
| `mampituba_concursos` | revisar→confirmado | MEJORA (FN) | `mampituba_concursos.json`: índice «Concursos Públicos», evento `001/2013`, edital de abertura y documentos. |
| `osorio_concursos` | revisar→confirmado | MEJORA (FN) | `osorio_concursos.json`: «Relação dos concursos públicos realizados» y entrada «Concurso 01/2019». |
| `pinheirinho_do_vale_concursos` | revisar→confirmado | MEJORA (FN) | `pinheirinho_do_vale_concursos.json`: página «Concursos Públicos» con entrada «Concurso Público 01/2023». |
| `canudos_do_vale_concursos` | confirmado→revisar | MEJORA (FP) | sólo links «Concursos Públicos 2021/2022/2023/2025», sin fila de evento. |
| `horizontina_concursos` | confirmado→revisar | MEJORA (FP) | sólo páginas por año «CONCURSO PÚBLICO 2021/2023/2025». |
| `ibiruba_concursos` | confirmado→revisar | MEJORA (FP) | `ibiruba_concursos.json`: únicamente «Concurso 2023», «Concurso 2018», «Concurso Público 2014» como navegación anual. |
| `imbe_concursos` | confirmado→revisar | MEJORA (FP) | cascarón con links «Concursos 2023», «Concurso 2025» y navegación hermana. |
| `imbe_processos` | confirmado→revisar | MEJORA (FP) | la captura sigue titulada «Concursos Publicos» y sólo expone una liga hermana PSS. |
| `itaara_concursos` | confirmado→revisar | MEJORA (FP) | noticia fechada: «Compartilhe… 7 fevereiro 2024 11:25… A semana começou com ótimas notícias». |
| `lajeado_do_bugre_processos` | confirmado→revisar | MEJORA (FP) | menú «Concursos e Processos Seletivos 2026…2015», sin eventos en la página. |
| `poco_das_antas_concursos` | confirmado→revisar | MEJORA (FP) | «Até o dia 21/05/2026 não foi realizado Concurso Público»; sólo hay un PSS. |
| `poco_das_antas_processos` | confirmado→revisar | MEJORA (FP) | un único PSS 01/2026 gobierna `VER ANEXOS` y todos sus documentos. |
| `lagoa_bonita_do_sul_processos` | confirmado→revisar | neutro/esperado | `lagoa_bonita_do_sul_processos.json`: subpágina «PROCESSOS SELETIVOS 2026», pestaña `ARQUIVOS`, pero captura incompleta «Por favor, aguarde…» sin filas/archivos recuperados. |

Comprobación de magnitud: sólo **10/618** se degradan; nueve son FP demostrables y uno es extracción incompleta. Los 301 buckets confirmados por baseline que no pertenecen a esos casos siguen confirmados.

## Justificación independiente de fixtures

El diff de ambos fixtures cambia exclusivamente:

```diff
-  "label": "fp",
-  "expected": "revisar",
+  "label": "tp",
+  "expected": "confirmar",
```

- **Anta Gorda/C — se conserva.** `authority_first/data/golden_set_v1.csv`, fila Anta Gorda, define como URL correcta `https://www.antagorda.rs.gov.br/concurso/categoria/25/concurso/`, `requiere_revision_humana=no`, y explica que es la categoría interna de concursos. El fixture usa exactamente esa URL y muestra la tabla `Nº/ANO MODALIDADE OBJETO…` con una fila. La etiqueta anterior contradecía el golden independiente y la regla de que una entrada basta.
- **Nova Boa Vista/C — se conserva.** No está en el golden. La verdad independiente está en `data/fase2/municipios_rs_local.csv`, fila 275: URL `https://novaboavista.rs.gov.br/pt_BR/concursos`, confianza `confirmado`, razón «Página índice combinada para concursos e processos seletivos». El corpus `nova_boa_vista_concursos.json` confirma el contenido real: título «Concursos e Processos Seletivos» y múltiples certámenes, entre ellos «Concurso Público Nº 001/2016» y «Concurso Público Nº 002/2016», además de concursos 2014. La expectativa previa `FP/revisar` era incompatible con esas dos fuentes.

No se revirtió ninguno de los dos fixtures porque ambos quedan respaldados sin usar la salida del código nuevo como autoridad.

## Casos finales en revisión

- Ambigüedad humana en la cola de 31: `poco_das_antas_concursos` — la página afirma que no hubo concurso y enumera un PSS; queda `nao_encontrado`/`revisar`, no se inventa índice.
- Rechazos que externamente conservan confianza `revisar`: las 10 instancias FP de noticia, menú y detalle de la tabla anterior. Internamente llevan estado discreto explicable.
- Contenido incompleto dentro de los 31: ninguno. La rama `content_complete=false` queda cubierta por prueba sintética y requiere validación local para señales HTTP/SPA no persistidas.

## Integridad de la implementación

- No se modificó ningún CSV ni el corpus run497.
- No hubo tráfico de red ni se ejecutaron chunks 5/6.
- No hubo escritura en `.git` ni comandos `add/commit/push`.
- El scratch ANTES permanece separado y no se añadió a Git.
