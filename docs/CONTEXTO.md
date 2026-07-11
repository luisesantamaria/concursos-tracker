# Contexto do Projeto

Concursos Tracker busca construir uma base confiavel de concursos publicos e processos seletivos do Rio Grande do Sul. O objetivo final e um motor de dados para uma app web de matching para concurseiros — nao apenas um scraper.

O usuario se registra com seu perfil (escolaridade, profissao, cidade + radio de km, salario minimo aceitavel) e a app faz duas coisas: (1) mostra concursos/PSS elegiveis para esse perfil, (2) envia alertas de ciclo de vida (gabarito, resultado, nomeacao).

## Evolucao

O projeto comecou usando Ache Concursos como fonte principal para descobrir concursos RS. Isso trouxe cobertura rapida, mas mostrou limites:

- Ache concentra concursos recentes e nem sempre cobre processos seletivos municipais.
- Ache nao cobre concursos antigos ainda validos para convocacoes/nomeacoes.
- Portais radar podem apontar links uteis, mas nao sao fonte final.

A arquitetura mudou para **authority-first**:

1. Primeiro fontes oficiais de banca.
2. Depois prefeitura/site oficial do orgao.
3. Depois diarios oficiais/FAMURS/portais municipais.
4. Por ultimo Ache/PCI/outros radares como auditoria de cobertura.

Depois, o pipeline de descoberta municipal evoluiu de um sistema de probes de URLs adivinhadas para una **cascata de 5 tiers** (site oficial → links gratuitos → grounding → adjudicador determinista + IA seletora → agente de navegacao Playwright).

Uma licao critica dessa evolucao: o sistema tentou usar um scorer numerico com ~50 constantes magicas para escolher entre URLs candidatas. Isso recriou o mesmo problema que o projeto fugiu — complexidade inmanejavel onde cada ajuste quebrava outro caso. O scorer foi abolido e substituido por decisoes discretas + Gemini como seletor inteligente (ai_pick_best).

## Modelo Mental

O projeto separa:

- `Concurso`: entidade mae, como Prefeitura X - Concurso Publico n. 01/2026.
- `Evento`: documento ou ato ligado ao concurso, como edital de abertura, retificacao, gabarito, classificacao, homologacao ou convocacao.

Essa separacao e importante porque bancas cobrem bem abertura ate resultado, enquanto prefeitura/diario cobrem melhor homologacao, convocacao, nomeacao e posse.

## Fases do Pipeline

O pipeline de descoberta municipal tem duas fases distintas que NAO devem ser misturadas:

**Fase atual (descoberta de recursos):** encontrar a pagina indice/listado estavel de concursos e PSS de cada municipio. Saida: URL da pagina de categoria, nao editais individuais.

**Proxima fase (scanner de indices):** entrar em cada pagina indice e extrair os editais/eventos individuais para construir o dataset de concursos com scraping.

## Estado Atual

- O piloto e apenas RS.
- `authority_first/` e a implementacao canonica.
- Um golden set de 24 municipios foi construido a mao para medir precisao/cobertura.
- O golden set revelou que ~20% dos municipios precisam de revisao humana — esse e o teto esperado de automacao, nao um bug.
- Gemini Tier 3 e usado somente como seletor inteligente entre candidatas ya
  adjudicadas; no confirma ni reclassifica dimensiones.
- Playwright esta implementado como ultimo recurso dirigido; su `EvidenceSnapshot`
  se reutiliza sin un segundo GET y queda marcado `evidence_state=renderizada`.
- A metrica principal e precisao (zero falsos positivos), nao cobertura.

## Tipos de Portal Conhecidos (do golden set)

O golden set de 24 municipios revelou 15+ arquiteturas distintas de portal:
- `.rs.gov.br` com menus simples (facil)
- `.atende.net` com transparencia delegada (Acegua)
- Portal delegado por IP (`multi24`, Ararica)
- Portal oxy.elotech (Anta Gorda)
- `pg.php` com subareas (Andre da Rocha)
- Portal com hash base64 na URL (Sao Leopoldo)
- Combobox que precisa selecionar para aparecer conteudo (Pelotas)
- Portal embebido que exige clicar em "consultar" (Gravatai)
- PSS listados como noticias (Canoas)
- Pagina combinada (mesma URL para concursos e PSS)
- Hermanas ambiguas (Arambare: tres opcoes de PSS no menu)

Essa variedade confirma que regras hardcodeadas por tipo de portal nao convergem. A solucao e verificacao por conteudo + selecao por IA.

## Pendencias Claras

- Mantener `ai_pick_best` como selector sem scorer numerico.
- Mantener la deteccion JS y el fallback Playwright dirigido (Tier 4).
- Cachear resultados de grounding por municipio para reprodutibilidade.
- Escalar o golden set conforme novas letras do alfabeto sao processadas.
- Scanner de indices (proxima fase): extrair editais individuais das paginas indice.
- Ampliar los tests automatizados a nuevos tipos de portal; el contrato estructural ya tiene matriz 10/10.
- Estabilizar export automatico para Google Sheets.

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
permanece con `decision+note`. Un `EvidenceSnapshot` de Playwright conserva
`renderizada` y no provoca un segundo GET.

La cadena ejecutable única es `CandidateRecord -> SelectedResource ->
FinalDecision`. `CandidateRecord` y `EvidenceSnapshot` son profundamente
inmutables: autoridad, identidad, rol, estado de evidencia, bucket, decisión y
razón se calculan una vez, antes de Tier 3. Tier 3 recibe solo records elegibles
y devuelve un `candidate_id`; un ID inexistente/incompatible o una selección no
resuelta produce `revisar` con razón, sin modificar el record.

`candidate_id` v1 es `v1:` + SHA-1 de JSON determinista con URL final normalizada
(host minúsculo/sin `www`, fragmento descartado, slash final removido y query
ordenada), source, tier, municipio normalizado, bucket adjudicado y SHA-1 del
snapshot completo. Distingue buckets/capturas y sirve solo para auditoría, nunca
para reconstruir records. Redirect/canonical conserva `requested_url` y
`final_url`; autoridad e identidad se evalúan únicamente sobre la final y su
contenido.

La instancia exacta seleccionada llega a `FinalDecision`; no hay refetch ni
segunda adjudicación batch. La compatibilidad URL-only obtiene una evidencia una
vez, construye un record mediante el mismo adjudicador central y siempre emite
razón. El CSV no cambia de esquema: URL/confianza/tier siguen en sus columnas y
provenance (`candidate_id`, tier, requested/final) + razón se serializan en
`razao`/`notes`. Telemetría JSON estable por candidato y bucket usa el logger
`fase2.cascade` en stderr, configurable con `FASE2_LOG_LEVEL`.

`pagina_generica_rechazada` era solo una constante Tier 3 sin consumidores ni
veredictos en el corpus. Se plegó a `nao_encontrado/revisar` según estructura;
el replay de 618 fixtures no mostró ningún flip atribuible a ese nombre.

Estado: cadena única y canario Barros Cassal verdes offline, matriz contractual
10/10, suite completa verde y replay run497 sin flips frente a 2b0dc11. Los
chunks reales 5/6 siguen fuera de alcance de este cambio.
