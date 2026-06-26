# Contexto do Projeto

Concursos Tracker busca construir uma base confiavel de concursos publicos e processos seletivos do Rio Grande do Sul. O objetivo inicial e mapear fontes oficiais, links de paginas oficiais e PDFs de edital de abertura. Depois, o projeto deve acompanhar eventos do ciclo de vida: retificacoes, gabaritos, resultados, homologacoes, convocacoes, nomeacoes e posse.

## Evolucao

O projeto comecou usando Ache Concursos como fonte principal para descobrir concursos RS. Isso trouxe cobertura rapida, mas mostrou limites:

- Ache concentra muitos concursos recentes e nem sempre cobre processos seletivos municipais.
- Ache nao cobre bem concursos antigos ainda validos para convocacoes/nomeacoes.
- Portais radar podem apontar links uteis, mas nao sao fonte final.
- A mesma prefeitura pode ter mais de um edital ativo; cada evento deve virar uma linha/evento separado quando necessario.

A arquitetura mudou para **authority-first**:

1. Primeiro fontes oficiais de banca.
2. Depois prefeitura/site oficial do orgao.
3. Depois diarios oficiais/FAMURS/portais municipais.
4. Por ultimo Ache/PCI/outros radares como auditoria de cobertura.

## Modelo Mental

O projeto separa:

- `Concurso`: entidade mae, como Prefeitura X - Concurso Publico n. 01/2026.
- `Evento`: documento ou ato ligado ao concurso, como edital de abertura, retificacao, gabarito, classificacao, homologacao ou convocacao.

Essa separacao e importante porque bancas cobrem bem abertura ate resultado, enquanto prefeitura/diario cobrem melhor homologacao, convocacao, nomeacao e posse.

## Estado Atual

- O piloto e apenas RS.
- `authority_first/` e a implementacao canonica.
- Existem experimentos e outputs antigos em `laboratorio/`, `scripts/`, `data/`, `logs/` e `output/`.
- Gemini tem sido usado como verificador/fallback, especialmente para pesquisa profunda de municipios e revisao de linhas duvidosas.
- Um golden set foi introduzido para medir precisao/cobertura e evitar regressao.

## Pendencias Claras

- Formalizar testes automatizados.
- Estabilizar export automatico para Google Sheets.
- Consolidar quais outputs curados devem ser versionados.
- Completar camada de municipios e diarios.
- Definir uma estrategia final para rate limits e sites que exigem navegador.
