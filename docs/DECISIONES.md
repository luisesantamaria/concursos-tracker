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

Decisao: manter um conjunto de exemplos curados para medir cobertura/precisao.

Motivo: exemplos individuais nao devem virar hardcode. Eles devem ensinar a regra e proteger contra regressao.

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
