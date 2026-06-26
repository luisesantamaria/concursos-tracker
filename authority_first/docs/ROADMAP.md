# Roadmap - Authority First RS

## Fase A - Esqueleto y contrato de scope

- Crear estructura V2.
- Declarar matriz fuente x evento.
- Crear filtro duro RS.
- Copiar V1 Ache-first a laboratorio.

## Fase A2 - Tabla base MVP

Antes del timeline completo de eventos, crear `concursos_base_rs`.

Columnas esenciales:

- semaforo;
- tipo (`concurso_publico` / `processo_seletivo`);
- orgao;
- municipio;
- numero;
- banca;
- pagina_oficial;
- edital_abertura_url.

Esta tabla solo busca la base oficial del certamen y el edital de abertura. Rectificacoes, gabaritos, resultados, homologacoes y nomeacoes quedan para fases posteriores.

## Fase B - Crawlers de bancas RS

Prioridad:

1. Legalle (`portal.institutolegalle.org.br` y `portal.editais.legalleconcursos.com.br`)
2. Fundacao La Salle
3. Fundatec
4. Quadrix
5. Objetiva
6. Cebraspe
7. Selecao/Fenix/portales anexos usados por prefeituras

Cada crawler debe producir eventos, no filas tipo Excel:

- edital_abertura
- retificacao
- cronograma
- gabarito
- resultado_classificados

## Fase C - Sites de prefeitura

Usar `sites_municipios_rs.csv` como registro oficial de municipios.

Objetivo:

- procesos seletivos que no salen en bancas;
- paginas de concursos municipales;
- convocacoes y listas locales;
- links a PDFs o paginas oficiales.

## Fase D - Diario municipal/FAMURS

Objetivo:

- homologacao;
- convocacao;
- nomeacao;
- posse;
- concursos antiguos todavia vigentes.

## Fase E - Normalizacion

Resolver:

- identidad de concurso;
- tipo (`concurso_publico` / `processo_seletivo`);
- edital_num;
- orgao;
- municipio;
- banca;
- estado del ciclo.

## Fase F - Auditor Ache

Ache deja de ser fuente principal y se convierte en comparador:

- concursos en Ache que faltan en el master;
- concursos en master que Ache no tiene;
- falsos positivos;
- recall por banca/municipio.
