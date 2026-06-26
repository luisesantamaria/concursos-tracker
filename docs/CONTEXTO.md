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

Depois, o pipeline de descoberta municipal evoluiu de um sistema de probes de URLs adivinhadas para uma **cascata de 5 tiers** (site oficial → links gratuitos → grounding → IA verificadora/seletora → agente de navegacao Playwright).

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
- `scripts/` e a implementacao canonica.
- Um golden set de 24 municipios foi construido a mao para medir precisao/cobertura.
- O golden set revelou que ~20% dos municipios precisam de revisao humana — esse e o teto esperado de automacao, nao um bug.
- Gemini e usado como verificador/fallback e como seletor inteligente entre candidatas validas.
- Playwright esta planejado como ultimo recurso para sites com JS ou botoes que saltam para destinos imprevisiveis, mas so sera implementado apos medir quantos municipios realmente precisam dele.
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

- Implementar ai_pick_best para substituir o scorer numerico.
- Implementar deteccao de JS e fallback Playwright dirigido (Tier 4).
- Cachear resultados de grounding por municipio para reprodutibilidade.
- Escalar o golden set conforme novas letras do alfabeto sao processadas.
- Scanner de indices (proxima fase): extrair editais individuais das paginas indice.
- Formalizar testes automatizados.
- Estabilizar export automatico para Google Sheets.
