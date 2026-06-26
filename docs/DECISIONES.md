# Decisoes Tecnicas

## 1. Scope RS Primeiro

Decisao: o piloto cobre somente Rio Grande do Sul.

Motivo: reduz ambiguidade, permite calibrar regras e evita misturar concursos federais/nacionais apenas porque tem prova no RS.

## 2. Authority-First

Decisao: a fonte final deve ser autoridade oficial, nao portal radar.

Ordem:

1. Banca.
2. Prefeitura/orgao.
3. Diario/FAMURS.
4. Ache/PCI/outros como radar/auditoria.

Motivo: uma fonte radar pode ter links uteis, mas nao deve validar sozinha um concurso.

## 3. Separar Concurso e Evento

Decisao: modelar Concurso como entidade mae e Evento como documentos/atos filhos.

Motivo: bancas cobrem abertura ate resultado; prefeitura/diario cobrem homologacao, convocacao, nomeacao e posse. Uma unica tabela plana perde essa diferenca.

## 4. Gemini como Verificador/Fallback

Decisao: usar Gemini 2.5 Flash para verificar e investigar quando as regras falham ou quando e necessario revisar uma rota.

Motivo: muitas prefeituras escondem links em menus, portais de transparencia ou sistemas externos. IA ajuda, mas a evidencia final continua sendo URL oficial e conteudo da pagina.

Observacao: Flash Lite foi evitado em corridas recentes por erros de disponibilidade.

## 5. Golden Set como Rubrica

Decisao: manter um conjunto de 24 municipios verificados a mao como ground truth independente.

Motivo: exemplos individuais nao devem virar hardcode. O golden set mede precisao e cobertura em cada mudanca, convertendo "acho que melhorou" em numeros falsaveis. Sem isso, cada ajuste e uma aposta cega.

Regras do golden set:
- Construido por humano, nunca gerado pelo pipeline.
- Inclui municipios faceis, portais delegados (IP), hermanas ambiguas, sites mudados, portais externos com hash, e casos que exigem revisao humana.
- ~20% dos municipios sao marcados como `revisar_humano` — esse e o teto esperado de automacao.
- A metrica a otimizar e precisao (zero falsos positivos), nao cobertura.

## 6. CSV + Google Sheets para Revisao

Decisao: CSV e o formato base; Google Sheets e a superficie de revisao manual.

Motivo: CSV e simples para pipeline; Sheets facilita inspeccionar links, comentarios e semaforos.

Pendiente: estabilizar comando/script oficial de upload para Google Sheets.

## 7. Infra Local/Cloud

Decisao: Cloud Run/RunPod/local LLM ficam como experimentos. Gemini API e a rota pratica atual para verificacao IA.

Motivo: RunPod teve fila/custo imprevisivel; Cloud Run GPU teve complexidade de deploy/model serving; Gemini resolveu mais rapido a camada de auditoria.

## 8. Nao Versionar Outputs Gerados

Decisao: logs, outputs, exports, modelos e credenciais ficam fora do Git.

Motivo: reduzir risco de segredo, manter repo leve e evitar ruido.

## 9. Abolir Scorer Numerico

Decisao: eliminar o sistema de pontuacao numerica (candidate_page_quality, bucket_dominance_score, process_family_score, source_label_score, etc.) com ~50 constantes magicas.

Motivo: o scorer recriou exatamente o monstro do qual o projeto fugiu. Cada ajuste (+150, -220, -120) arrumava um caso e quebrava outro. O bug da normalizacao que perdia `=` de `secao=dinamico` era o canario — um sistema complexo demais para raciocinar. Penalizar `multi24` ou IPs crus e hardcodear rarezas de UM provedor de portal; quando aparecem outros provedores, o scorer falha e precisa de mais constantes. Esse caminho nao converge.

Substituicao:
- Verificacao deterministica (sinais de conteudo) decide se uma pagina e valida.
- Gemini `ai_pick_best` decide qual candidata valida e a melhor pagina indice.
- Decisoes discretas (indice_oficial, revisar, nao_encontrado) substituem numeros.

## 10. Cascata de 5 Tiers

Decisao: o pipeline de descoberta municipal usa uma cascata que gasta ferramentas caras somente quando as baratas falham.

Tier 0: Encontrar dominio oficial da prefeitura.
Tier 1: Links gratuitos do HTML (menus, anchors, sitemap, transparencia).
Tier 2: Gemini com Google Search grounding (so se Tier 1 nao completou).
Tier 3: Gemini verificador/seletor — classifica candidatas e escolhe a melhor.
Tier 4: Agente de navegacao Playwright — ultimo recurso, dirigido por menus.

Motivo: grounding custa ~$35/1000 prompts e Playwright e lento (~3-5s/pagina). A cascata gratis-primeiro e pura otimizacao de custo sem penalizacao de exatidao. A exatidao vem do verificador, nao de qual tier descobriu a URL.

## 11. Precisao sobre Cobertura

Decisao: zero falsos positivos importa mais que cobertura alta.

Motivo: 3/5 limpo vale mais que 5/5 com uma URL errada. Para uma base de dados, precisao e a metrica que importa. Huecos honestos podem ser preenchidos depois ou manualmente. Um pipeline que diz "nao sei, revise" e superior a um que inventa uma URL.

Observacao: ~20% dos municipios genuinamente precisam de revisao humana (combobox, portal embebido, hash base64 na URL, anexos espalhados). A meta nao e "100% automatico" — e "automatizar os ~80% limpos e reconhecer os ~20% que precisam de olhos humanos".

## 12. Playwright Dirigido, nao Cego

Decisao: quando Playwright e necessario, navegar pelos menus relevantes (Publicacoes, Concursos, Transparencia), nao rastrear todo o site.

Motivo: "todo o site" e centenas de paginas irrelevantes, lento (5-20 min/municipio) e mais ruidoso (mais superficie para falsos positivos). A navegacao dirigida faz o que um humano faria: seguir os botoes cujo texto indica concursos/PSS e ver para onde o navegador vai. Isso resolve Ararica (botao salta para IP multi24) sem rastrear 200 paginas de contratos.

Regras tecnicas:
- Reusar UMA instancia de browser durante toda a corrida (nao lanca/fecha por URL).
- Detectar necessidade de JS antes de lancar: `<div id="root">`, `<div id="app">`, `<body>` vazio com muitos `<script>`.
- So dispara quando Tiers 0-3 falharam E a pagina parece renderizada por JS ou tem botoes que saltam para destinos imprevisiveis.

## 13. Nao Hardcodear Padroes de Provedor

Decisao: nao adicionar regras especificas para multi24, secao=dinamico, IPs crus, subareas especificas, etc. no verificador/seletor.

Motivo: cada regra e deuda que se paga nas letras B-Z. O municipio de Andre da Rocha precisa de subarea=19 e nao subarea=13, mas hardcodear subareas e voltar as URLs fixas que quebram. Quando escalar para nivel nacional, nao ha como recalibrar constantes por estado.

Substituicao: o verificador determinístico valida por conteudo (sinais de selecao publica, listado de editais). O seletor IA (`ai_pick_best`) escolhe entre candidatas validas por compreensao, nao por padrao hardcodeado.

## 14. Headers de Navegador Real

Decisao: usar User-Agent de Chrome real e headers Accept corretos nas sessoes de requests.

Motivo: o UA "concursos-rs-grounded/0.1" grita "sou um bot" e servidores municipais quisquillosos devolvem HTTP 406 (Ametista). Com UA de navegador real, essas paginas se deixam ler. Alto impacto, risco zero.

## 15. Cache de Grounding por Municipio

Decisao: cachear resultados de grounding em disco por municipio.

Motivo: grounding com temperatura 1.0 + validador IA significam que duas corridas do mesmo municipio podem dar URLs distintas. Cachear garante reprodutibilidade, economiza custo em re-runs, e estabiliza o golden set.

## 16. Fase Atual: Paginas Indice, nao Editais

Decisao: a fase atual descobre a pagina indice/listado estavel de concursos e PSS de cada municipio. NAO extrai editais individuais, PDFs, nem detalhes de eventos.

Motivo: separar as fases evita que o verificador se confunda entre "encontrar a secao certa" e "analisar riqueza de conteudo". A riqueza de eventos e, no maximo, um desempate fraco — o que importa e a evidencia do menu e que a URL seja uma raiz de categoria.

Regra: nunca aceitar edital_pdf, pagina de um edital especifico, /detalhe/452/, anexo, cronograma, retificacao, licitacao, ou concurso cultural como URL de bucket. Se so encontrou um detalhe mas nao o indice, status = revisar.
