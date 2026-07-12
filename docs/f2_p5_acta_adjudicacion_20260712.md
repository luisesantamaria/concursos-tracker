# F2.P5 — Acta de adjudicación humana (Luis) — 12-jul-2026

Cierre del golden RS. Método: pre-auditoría de 14 agentes (citas verificadas
contra snapshot congelado + curl) + **auditoría manual en navegador por Fable
sobre las 9 URLs**, refrendada por Luis. Gravataí resuelto con CSV oficial
descargado por Luis; Porto Alegre adjudicado por Luis (acepta "em andamento").

## Veredicto: 14/14 RATIFICADAS — CERO falsos positivos

| Unidad | URL | Evidencia navegador | Veredicto |
|---|---|---|---|
| Alvorada / PSS | /documentos | Índice "Editais e Documentos", filtro por año 2013-2026, 40 docs, PSS reales (Técnico em Informática 07/2026) | RATIFICADA |
| Bento Gonçalves / CP | oxy.elotech .../publicacoes/28 | SPA elotech renderizó bloque CONCURSOS + buscador, últ. atual. 11/07/2026 | RATIFICADA |
| Caxias do Sul / CP | .../concursos | Sección oficial con pestañas Vigentes/Encerrados/Nomeação, CP 01/2026 + anexos (Fundatec) | RATIFICADA |
| Itaqui / PSS | ?action=concursos | Índice cronológico, 164 menciones PSS, activo hasta 09/2026 con ciclo completo | RATIFICADA |
| Itaqui / CP | ?action=concursos | Combinada; CP real presente pero antiguo (2012). NOTA: contenido CP escaso; coincide con golden (misma URL ambos buckets) | RATIFICADA (con nota) |
| Novo Hamburgo / CP | /concursos | Tabla Concurso/Situação con filtro, Edital 01/2026 Inscrições abertas + histórico a 2015 | RATIFICADA |
| Novo Hamburgo / PSS | /processos-seletivos | Tabla Processo/Situação con filtro + paginación, PS 05/2026 | RATIFICADA |
| Pelotas / CP | .com.br/oportunidades/concursos-estagios | Índice Vigência/Encerrados (Edital 438-2025 +) | RATIFICADA |
| Pelotas / PSS | .../selecao-publica-simplificada | Combobox poblado con 7 editais 2026 (407/408) + histórico | RATIFICADA (combobox, per golden) |
| Aceguá / PSS | atende.net/.../processos-seletivos | Índice completo con ciclo entero (Edital PSS 001/2026 → homologação → resultado), 48 docs | RATIFICADA |
| Gravataí / CP | atende .../concursos-e-seletivos | CSV oficial "Dados Abertos": Concurso Público 02/2025 docentes + estructura combinada | RATIFICADA (CSV) |
| Gravataí / PSS | atende .../concursos-e-seletivos | CSV oficial: 21 Processos Seletivos 2016-2026 (últ. nº2/nº3 de 2026) | RATIFICADA (CSV) |
| Porto Alegre / CP | prefeitura.poa.br/smap/concursos-em-andamento | Índice vivo (CP 866-874); Luis acepta "em andamento" como índice válido; "homologados" = fuente complementaria de eventos para F5 | RATIFICADA (decisión Luis) |
| Porto Alegre / PSS | prefeitura.poa.br/smap/processos-seletivos-em-andamento | Índice PSS em andamento (Dengue, Inverno, Residência) | RATIFICADA (decisión Luis) |

## Decisiones de oráculo registradas
1. **Porto Alegre**: el índice "em andamento" se acepta como confirmación del bucket (es donde aparecen los certames nuevos = lo que las alertas necesitan). "Homologados/vigentes" pasa a ser fuente complementaria de eventos en el monitoreo (F5), no requisito de confirmación.
2. **Gravataí**: portal atende cuyo listado no renderiza pasivamente; el "Dados Abertos" (CSV oficial) prueba el índice combinado real. Caso guía para F3.P5 (exploración interactiva).
3. **Itaqui/CP**: match con golden aunque el contenido de concursos públicos sea escaso (municipio casi solo hace PSS); no es sobre-confirmación, la URL es la sección oficial correcta.

## Resultado
- **0 falsos positivos** en las 30 confirmaciones de R4 (ninguna URL confirmada difiere del golden — hecho del differential) — y las 14 dudosas/nuevas ratificadas con auditoría manual profunda sin encontrar FP.
- Bug abierto no bloqueante: **Canoas/PSS** (el motor no confirmó el feed noticias_tag pese al fix; investigar post-P5, candidato a F3.P5).
- **F2.P5 CERRADO.** Siguiente: F2.P6 (holdout ciego de 50) — requiere autorización de Luis.
