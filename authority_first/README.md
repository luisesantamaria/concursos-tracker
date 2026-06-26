# Authority First Pipeline

Pipeline V2 para Concursos Tracker.

Este arbol es el nuevo centro del proyecto. La idea es dejar de usar Ache Concursos como tabla principal y pasar a un modelo orientado por autoridad:

- banca primero para edital, retificacao, cronograma, gabarito y resultado;
- prefeitura cuando no hay banca o para processos seletivos locales;
- diario municipal/FAMURS para homologacao, convocacao, nomeacao y posse;
- Ache solo como radar y auditor de cobertura.

## Scope actual

Piloto unico: Rio Grande do Sul (`RS`).

Regla dura: aunque una banca publique concursos de todo Brasil, este pipeline solo puede guardar candidatos con evidencia de RS. La evidencia se valida contra `data/sites_municipios_rs.csv` y las reglas de `config/scope_rs.yaml`.

## Salidas objetivo

- `data/normalized/concursos_base_rs.csv`: primera tabla MVP con una fila por concurso/PSS y solo los links base.
- `data/raw/*`: documentos/eventos crudos por fuente.
- `data/normalized/concursos_master.csv`: entidad madre para matching.
- `data/normalized/eventos_master.csv`: timeline de eventos para alertas.
- `data/normalized/documentos_master.csv`: inventario documental con hash y fuente.
- `data/exports/*`: Excel/Sheets para revision humana.

## MVP actual

Antes de modelar eventos del ciclo completo, construir `concursos_base_rs`:

- `tipo`: `concurso_publico` o `processo_seletivo`;
- `orgao`;
- `municipio`;
- `numero`;
- `pagina_oficial`: pagina estable del certamen en banca o prefeitura;
- `edital_abertura_url`: link directo al edital de abertura.

Ver `docs/BASE_TABLE_SPEC.md`.
