# Concursos Tracker

## O que é este projeto

Um portal/app de avisos de **concursos públicos e processos seletivos** de
todo o Brasil. O usuário cadastra seu perfil — escolaridade, profissão,
cidade (com raio de distância ou cidades para onde aceitaria se mudar) e
salário mínimo — e o portal mostra os certames elegíveis para ele e envia
alertas do ciclo de vida: novo edital, retificação, inscrições encerrando,
convocação, homologação, nomeação.

## O propósito

Quem presta concurso hoje precisa vigiar dezenas de sites de bancas, portais
de prefeituras e diários oficiais — ou depender de agregadores incompletos,
atrasados e sem link para a fonte. O propósito deste projeto é que **nenhuma
oportunidade pública passe despercebida para quem ela serve**, com um padrão
que os agregadores não têm: **zero falsos positivos e toda afirmação
respaldada por evidência verificável** (cada dado do portal aponta para o
documento oficial que o prova). Um aviso errado custa ao usuário uma taxa de
inscrição ou uma mudança de cidade — por isso precisão vale mais que
cobertura, e a abstenção honesta ("revisar") é preferível a um chute.

## O que vamos fazer

O projeto tem duas metades, e a difícil é a primeira:

**1. O motor de dados (o fosso competitivo).** Descobrir, verificar e
monitorar as fontes oficiais dos ~5.570 municípios + bancas + diários:

- **Descobrir**: onde cada município/banca publica seus certames (a URL
  estável do índice). Verificar isso à mão para 5.570 municípios é inviável —
  por isso a descoberta é automatizada (padrões por plataforma de CMS,
  registro de domínios oficiais, busca dirigida) e a **decisão final é de um
  juiz de IA com citações literais verificadas por código** (motor V2), com
  meta de 0 falsos positivos.
- **Monitorar**: releituras periódicas das fontes confirmadas para detectar
  novos editais e eventos — priorizadas por um sinal de atividade nacional
  (Querido Diário, bancas, radares), não por força bruta.
- **Extrair e consolidar**: transformar páginas e PDFs em certames
  estruturados (cargos, escolaridade, remuneração, cronograma) e unir as
  menções de várias fontes na mesma entidade (resolução de identidade), com
  a linha do tempo completa de cada certame.

**2. O produto.** O portal/app com perfis, filtros, matching geográfico e
alertas — construído sobre o motor (ver `MANUAL_APP.md`).

## Como vamos fazer (os princípios que não se negociam)

- **Fontes de verdade convergentes**: nenhuma fonte manda em tudo. A banca é
  a mais rica do ciclo ativo *quando existe* (a maioria dos PSS e alguns
  concursos nunca passam por banca); a prefeitura é o publicador legal e
  muitas vezes a única fonte de PSS; o diário oficial é o registro com valor
  legal; os radares (Ache, PCI) só descobrem e auditam, nunca provam. A
  autoridade é atribuída **por tipo de fato** (matriz fonte × evento) e as
  fontes se corroboram.
- **IA adjudica conteúdo, código verifica fatos**: o certificador de IA lê a
  evidência congelada e cita trechos literais; o código verifica cada citação
  caractere por caractere, a autoridade/identidade do domínio e o estado da
  página. Nada é publicado sem passar por esse portão.
- **Precisão sobre cobertura**: 0 FP > cobertura alta; ~condição de parada:
  um único falso positivo detido interrompe a fase (protocolo STOP).
- **Verdade de campo humana**: golden sets construídos à mão são o oráculo de
  desenvolvimento; holdouts cegos medem a generalização antes de escalar; o
  sistema não aprende sozinho do oráculo — padrões entram só como fatos
  curados com proveniência humana.

## Onde estamos (2026-07-12)

- **Fase 1 (bancas RS)**: feita.
- **Fase 2 (descoberta municipal RS)**: motor V2 funcionando — no golden de
  36 unidades: 22 acertos, **0 falsos positivos**; sobre evidência idêntica,
  a IA acerta 22/23 contra 2/23 das heurísticas antigas. Suite: 419 testes
  verdes. Falta o fechamento (fixture envenenado, holdout de 50, corrida dos
  497) — ver `PLAN_MAESTRO.md`.
- O caminho completo por fases está no `ROADMAP.md`; o passo a passo
  executável com gates e ramas de falha, no `PLAN_MAESTRO.md`.

## Documentos (por ordem de leitura)

| Documento | O que contém |
|---|---|
| `README.md` | Este arquivo: o que é o projeto, propósito, como fazemos. |
| `ROADMAP.md` | O projeto inteiro dividido em fases: de onde viemos, onde estamos, para onde vamos. |
| `PLAN_MAESTRO.md` | O plano executável: cada passo com pré-requisito, ações, prova de sucesso e rama de falha. **Plano de registro.** |
| `MANUAL_IMPLEMENTACION.md` | Arquitetura do motor: 4 planos, modelo de dados canônico, funil de descoberta. |
| `MANUAL_APP.md` | Como construir o portal/app: stack, 9 etapas, LGPD, alertas. |
| `CLAUDE.md` | Regras operativas para agentes/humanos (intocáveis, comandos, disciplina). |
| `docs/` | Documentação técnica (arquitetura fase 2, runbook de corridas do Brasil, specs) e arquivo histórico (`docs/archivo/`). |

## Estrutura do repositório

```text
scripts/fase1_bancas/         Crawlers de bancas (RS).
scripts/fase2_municipios/     Cascata de descoberta municipal (V1, congelada)
  v2/                         Motor V2 de adjudicação (agentes, portão, eval,
                              registro de domínios, snapshot/citações, render).
scripts/eval/                 Avaliador golden + baseline V1 (protegido).
scripts/shared/               Escopo RS, perfil de navegador, waf guard.
config/                       Matriz de autoridade, schema, escopo (YAML).
data/                         Golden set, registros, saídas do pipeline.
docs/                         Docs técnicos + docs/archivo/ (histórico).
staging/                      Corridas de avaliação congeladas (gitignored).
```

## Instalação e comandos

Python 3.12+ (o venv das corridas vive no WSL: `.venv/bin/python`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium

# Suite de testes do motor V2
python -m pytest scripts/fase2_municipios/v2 -q
```

Os comandos de avaliação/corrida (golden live, comparação semântica,
avaliador legado) estão no `PLAN_MAESTRO.md` §0 com todas as flags. Regras de
segurança (nunca commitar segredos, corridas congeladas, protocolo STOP por
FP): `CLAUDE.md`.
