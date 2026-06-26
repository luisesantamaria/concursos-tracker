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

Tier 3 - Gemini verificador/seletor
  Recebe candidatas e faz decisoes discretas:
  - indice_oficial / indice_oficial_combinado
  - portal_externo_oficial
  - detalle_individual_rechazado
  - licitacao_rechazada / concurso_cultural_rechazado
  - nao_encontrado / revisar
  Quando ha multiplas candidatas validas: ai_pick_best escolhe
  por compreensao de conteudo, nao por pontuacao numerica.

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

### Pendencias desta fase

- [ ] Implementar ai_pick_best (substituir scorer numerico).
- [ ] Headers de navegador real (corrigir 406 anti-bot).
- [ ] Cache de grounding por municipio.
- [ ] Deteccao de JS e fallback Playwright dirigido (Tier 4).
- [ ] Escalar golden set com municipios de outras letras.

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
