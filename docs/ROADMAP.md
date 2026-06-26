# Roadmap

## Fase 0 - Higiene do Projeto

- Criar README, AGENTS, CLAUDE e docs raiz.
- Proteger segredos e outputs com `.gitignore`.
- Preparar repositorio privado GitHub.
- Pendiente: instalar Git/GitHub CLI na maquina atual ou fazer commit/push em outra maquina.

## Fase 1 - Camada de Bancas

Objetivo: coletar concursos/processos RS diretamente nas bancas.

Bancas conhecidas do laboratorio:

- Fundatec
- Legalle Concursos
- Instituto Legalle
- Fundacao La Salle
- Quadrix
- Objetiva
- Instituto Fenix / Selecao.net
- FAURGS
- Cebraspe
- FGV
- Cesgranrio e outras, com cuidado para nao incluir concursos federais/nacionais fora do escopo RS

Saida esperada:

- `tipo`
- `orgao`
- `municipio`
- `uf`
- `numero`
- `banca`
- `edital_pagina`
- `edital_pdf`
- `semaforo`
- comentario quando precisar revisar

## Fase 2 - Recursos Municipais

Objetivo: mapear, para cada municipio RS, as paginas oficiais onde ficam concursos e processos seletivos.

Estrutura desejada:

- `site_base`
- `url_concursos`
- `url_processos_seletivos`
- status e comentario de validacao

Regras:

- Preferir pagina agregadora oficial, nao edital individual.
- Comparar candidatas dentro do menu antes de aceitar a primeira URL valida.
- Taxonomia para processos seletivos inclui `Processo Seletivo Simplificado`, `Processo Seletivo`, `Selecao Publica`, `Contratacao Temporaria`, e casos equivalentes de selecao de pessoal.
- Se o crawler nao encontrar, usar Gemini como mini-investigador/verificador.

## Fase 3 - Diario/FAMURS/Publicacoes

Objetivo: cobrir homologacoes, convocacoes, nomeacoes e eventos administrativos que nao ficam na banca.

Pendencias:

- Definir estrategia para Diario Oficial dos Municipios/FAMURS.
- Resolver consultas por data quando o portal nao tem busca direta simples.
- Relacionar documentos do diario ao concurso/processo correto sem duplicar.

## Fase 4 - Scanner de Eventos

Objetivo: transformar paginas oficiais em eventos estruturados.

Eventos alvo:

- edital de abertura
- retificacao
- cronograma
- gabarito
- classificacao
- resultado
- homologacao
- convocacao
- nomeacao
- posse

## Fase 5 - PDFs, Hash e Extracao

Objetivo: baixar PDFs, hashear documentos, evitar duplicados e extrair metadados.

Pendencias:

- Definir storage final.
- Definir parser principal.
- Criar regras para PDF escaneado/OCR se necessario.

## Fase 6 - Normalizacao e Dedupe

Objetivo: criar tabela mestre confiavel.

Pendencias:

- Chave canonica para concurso/processo.
- Regras de merge entre banca, prefeitura, diario e radar.
- Golden-set/regression tests.

## Fase 7 - Produto e Alertas

Objetivo: interface e notificacoes.

Pendencias:

- Google Sheets/CSV curado.
- Dashboard ou app.
- Alertas por perfil do usuario.
- Atualizacao incremental diaria.
