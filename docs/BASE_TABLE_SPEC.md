# Base Table Spec - RS

Esta es la primera tabla util del pipeline.

Objetivo: construir una base limpia de concursos publicos y processos seletivos de RS, con la pagina oficial donde vive el certamen y el PDF directo del edital de abertura.

Por ahora NO se modelan rectificacoes, gabaritos, resultados, homologacoes ni nomeacoes. Esos seran eventos hijos en fases posteriores.

## Columnas MVP

| Columna | Descripcion |
|---|---|
| `semaforo` | `listo`, `revisar`, `no_encontrado`. |
| `tipo` | `concurso_publico` o `processo_seletivo`. |
| `orgao` | Organo responsable con nombre limpio y completo. |
| `municipio` | Municipio RS cuando aplique. |
| `uf` | Siempre `RS` en este piloto. |
| `numero` | Numero normalizado del edital/PSS, formato preferido `nº 01/2026`. |
| `banca` | Banca organizadora, si existe. |
| `pagina_oficial` | URL base oficial del certamen, donde aparecen edital de abertura y futuros documentos. Puede ser banca o prefeitura. |
| `edital_abertura_url` | Link directo al PDF o pagina del edital de abertura. |
| `fonte_primaria` | `banca`, `prefeitura` o `diario`. Para esta fase normalmente `banca` o `prefeitura`. |
| `fonte_radar` | Portal que descubrio el candidato, si aplica, por ejemplo `ache_concursos`. No verifica. |
| `radar_url` | URL de Ache u otro radar, si fue usado como pista. |
| `evidencia_rs` | Senales que justifican que la fila pertenece a RS. |
| `status_validacao` | Motivo breve de por que quedo listo/revisar/no_encontrado. |
| `last_checked` | Fecha/hora de ultima verificacion. |

## Reglas

1. `pagina_oficial` debe ser la pagina estable del certamen, no la homepage generica de la banca ni de la prefeitura.
2. Si hay banca, la pagina oficial preferida es la pagina del concurso en la banca.
3. Si no hay banca, usar pagina especifica de prefeitura para concursos/PSS.
4. Si solo hay diario, usarlo como respaldo, pero marcar `revisar` si no hay pagina estable del certamen.
5. `edital_abertura_url` debe apuntar al edital de abertura o documento equivalente. No aceptar cronograma, aviso, anexo, lista ni retificacao como edital base.
6. Si una misma prefeitura tiene dos editais distintos, son dos filas.
7. Si no hay evidencia dura de RS, la fila no entra.

