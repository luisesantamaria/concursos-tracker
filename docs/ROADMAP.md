# Roadmap

## Fase 0 - Higiene do Projeto ✅

- Criar README, AGENTS, CLAUDE e docs raiz.
- Proteger segredos e outputs com `.gitignore`.
- Preparar repositorio privado GitHub.

## Fase 1 - Camada de Bancas ✅

Objetivo: coletar concursos/processos RS diretamente nas bancas.

Bancas conhecidas: Fundatec, Legalle, Instituto Legalle, La Salle, Quadrix, Objetiva, Instituto Fenix/Selecao.net, FAURGS, Cebraspe, FGV, Cesgranrio.

Saida: tipo, orgao, municipio, uf, numero, banca, edital_pagina, edital_pdf, semaforo.

## Fase 2 - Descoberta de Recursos Municipais 🔄

Objetivo: mapear, para cada municipio RS, as paginas indice/listado oficiais onde ficam concursos e processos seletivos.

**IMPORTANTE:** Esta fase descobre a pagina de CATEGORIA/INDICE, nao editais individuais. A saida e a URL estavel onde a prefeitura lista todos os concursos ou PSS.

### Arquitetura: Cascata de 5 Tiers

```
Tier 0 - Site oficial
  Encontra ou confirma o dominio base da prefeitura.

Tier 1 - Links gratuitos
  Busca menus HTML, anchors, sitemap, portal da transparencia.
  Puro requests, sem IA, sem custo.

Tier 2 - Busca grounded (Gemini + Google)
  So se Tier 1 nao completou ambos buckets.
  Uma chamada por municipio com google_search.

Tier 3 - Gemini seletor
  Recebe CandidateRecords ya adjudicados deterministicamente:
  - indice_oficial / indice_oficial_combinado
  - portal_externo_oficial
  - detalle_individual_rechazado
  - licitacao_rechazada / concurso_cultural_rechazado
  - nao_encontrado / revisar
  Quando ha multiplas candidatas validas: ai_pick_best devuelve candidate_id
  por compreensao de conteudo, sem classificar/confirmar dimensoes e sem
  pontuacao numerica.

Tier 4 - Agente de navegacao (Playwright)
  Ultimo recurso. Abre o site em Chromium headless e navega
  pelos menus como humano — dirigido pelo texto dos botoes,
  NAO rastreamento cego de todo o site.
  So para municipios onde botoes saltam para destinos
  imprevisiveis (IP crudo, portal JS-only).
```

### Regras desta fase

- Preferir pagina agregadora oficial, nao edital individual.
- Comparar candidatas validas usando IA (ai_pick_best), nao scorer numerico.
- Taxonomia para processos seletivos inclui PSS, Processo Seletivo, Selecao Publica, Contratacao Temporaria.
- Se o pipeline nao tem certeza, status = revisar (melhor que inventar).
- Precisao sobre cobertura: zero falsos positivos e a prioridade.
- ~20% dos municipios precisam de revisao humana — isso e aceitavel.

### Medicao

- Golden set de 24 municipios verificados a mao.
- Script `medir_golden_set.py` mede precisao e cobertura por tipo de portal.
- Rodar apos QUALQUER mudanca no verificador ou seletor.

### Estado atual - 2026-07-11

Execucao ampla continua pausada, mas o contrato estrutural e a cauda de risco ja
foram validados offline. O proximo passo autorizado e apenas o canario isolado de
Barros Cassal, Boa Vista do Sul e Progresso; chunks 5-6 seguem sem execucao.

Familias de FP ja detectadas: noticias individuais classificadas como indice, paginas genericas de menu sem listagem real, e sobre-conteo por resultados duplicados/inflados. Depois de mapear a cauda, corrigir cada familia no pipeline e validar contra o golden set. So entao retomar fase 2 chunks 5-6.

### Pendencias desta fase

- [x] Implementar ai_pick_best sem scorer numerico.
- [x] Headers de navegador real para reduzir 406 anti-bot.
- [ ] Cache de grounding por municipio.
- [x] Deteccao de JS e fallback Playwright dirigido (Tier 4), con snapshot reutilizable.
- [ ] Escalar golden set com municipios de outras letras.
- [x] Auditar la cola de riesgo run497 y cerrar noticia/menu/detalle; overcount ya no decide por conteo.

## Fase 3 - Scanner de Indices

Objetivo: entrar em cada pagina indice descoberta na Fase 2 e extrair editais/eventos individuais com scraping.

Saida por evento: titulo, tipo_evento, url_documento, url_pdf, edital_num, data, hash, first_seen.

Esta e a fase onde se extrai a informacao de CADA concurso/PSS para construir o dataset. A Fase 2 deu a porta de entrada; a Fase 3 entra e cataloga o conteudo.

## Fase 4 - Diario/FAMURS/Publicacoes

Objetivo: cobrir homologacoes, convocacoes, nomeacoes e eventos administrativos que nao ficam na banca.

- Adapter do Diario Municipal FAMURS para municipios sem rota clara.
- Adapters dedicados para ~15 municipios grandes com DOM proprio.
- Querido Diario como respaldo onde FAMURS nao chegue.

## Fase 5 - PDFs, Hash e Extracao

Objetivo: baixar PDFs, hashear documentos, evitar duplicados e extrair metadados.

- Filtrar candidatos com sinais fortes e `.pdf`.
- SHA256 para dedup.
- PyMuPDF (texto) + pdfplumber (tablas).
- PDF escaneado → OCR tesseract so se necessario.

## Fase 6 - Classificacao + Regex de Campos

Extrair: orgao, municipio, banca, vagas (CR + cotas), salario, taxa, periodo de inscricoes, data das provas, escolaridade.

Gate para publicar: (a) "edital"/"processo seletivo" no nome, (b) fonte oficial, (c) inscricao futura ou passada < 60 dias.

## Fase 7 - Normalizacao e Dedupe

- Chave canonica para concurso/processo.
- Regras de merge entre banca, prefeitura, diario e radar.
- Tabela mestre `concursos_master`.

## Fase 8 - Produto e Alertas

- Interface web / app.
- Matching por perfil do usuario.
- Alertas de ciclo de vida.
- Atualizacao incremental diaria (cron nocturno).

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

La cadena única en memoria es `CandidateRecord -> SelectedResource ->
FinalDecision`. Record y snapshot son profundamente inmutables; todas las
dimensiones y la razón se adjudican una vez antes de Tier 3. La selección guarda
la instancia exacta, y cierre/batch deriva la decisión sin refetch ni
re-adjudicación. Legacy URL-only captura evidencia una vez y llama al mismo
adjudicador central; toda salida, incluso no-upgrade, tiene razón.

`candidate_id` v1 es `v1:` + SHA-1 de URL final normalizada (host minúsculo sin
`www`, sin fragmento/slash final, query ordenada), source, tier, municipio,
bucket y huella del snapshot. Es trazabilidad, no reconstrucción. Redirects
conservan requested/final y se evalúan por URL/contenido final. El CSV mantiene
su esquema; provenance mínima y razón van en `razao`/`notes`. Telemetría JSON por
candidato/bucket usa `fase2.cascade` a stderr y `FASE2_LOG_LEVEL`.

`pagina_generica_rechazada` era solo una constante Tier 3 sin consumidores ni
veredictos en el corpus. Se plegó a `nao_encontrado/revisar` según estructura;
el replay de 618 fixtures no mostró ningún flip atribuible a ese nombre.

Estado: cadena única y canario Barros Cassal verdes offline, matriz contractual
10/10, suite completa verde y replay run497 sin flips frente a 2b0dc11. Los
chunks reales 5/6 siguen fuera de alcance de este cambio.
