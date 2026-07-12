# F2.P6 — Acta del holdout ciego de 50 (12-jul-2026)

## Setup
50 municipios NO-golden, estratificados por plataforma (19 atende, 17 rs.gov.br,
10 desconocida, 4 multi24; 28 borderline), 88 unidades con URL candidata del
descubrimiento V1 SIN curación. Golden ciego (expectativas vacías). Corridas:
run_r1 (free, seed 2026071251) + run_retry_paid (gemini_policy free→pagado,
seed 2026071253; el fallback pagado funcionó: free 429 → paid ok).

## Resultado bruto
21/88 confirmadas (24%) · 42 revisar · 22 error→revisar · 3 nao_encontrado.
GATE de cobertura (revisión ≤30%): **NO CUMPLIDO** → rama SI-FALLA del plan
activada (el hueco es aguas arriba, no del adjudicador).

## Auditoría de las 21 confirmadas (7 lotes de pre-auditores anti-FP)
**17 RATIFICADAS limpias · 4 DUDA · 0 FP duros** (ningún municipio equivocado,
ninguna licitação-como-concurso, ninguna noticia-como-índice).
Las 4 dudas — RESUELTAS por verificación en navegador (12-jul, delegado por
Luis a Fable: "no me preguntes veredictos, sácalos tú"):
- **Canela CP+PSS = FALSO POSITIVO REAL (×2)**. Verificado en vivo: la
  búsqueda del portal de transparencia con filtros "Todos" (todas las
  entidades, años 2013-2026) devuelve "Nenhum registro encontrado" — cero
  ítems en la historia del módulo. Y el municipio SÍ publica sus certames:
  en canela.rs.gov.br/sitenovo/categorias-publicacoes/cat-publicacoes-legais/
  (categoría "Concurso / Processos Seletivos", filtro combobox) viven el PSS
  01/2026 (Legalle), el Concurso 01/2023 con convocatorias 2025 y el PSS de
  estagiários 2026. Confirmamos un módulo que la prefeitura nunca alimenta:
  un usuario suscrito jamás recibiría alerta. → R-T1 STOP: corrección general
  (regla anti-índice-vacío: las citas de bucket deben anclar en ítems, nunca
  en mensajes de ausencia) + caso al fixture envenenado.
- **Montenegro/CP = RATIFICADA**. Verificado en vivo: índice real con
  Concurso Público 2016, 2019 y 2025. La cita anclada en menú es nota de
  calidad, no FP.
- **Itacurubi/PSS = RATIFICADA**. Verificado en vivo: los PSS VIGENTES
  (Edital 01/2026 PIM, PSS 001/2026 cozinheira) se publican en /site/editais;
  /site/concursos solo tiene PSS de 2015. El motor eligió la página correcta.
**Precisión final del holdout: 19/21 (90.5%), 2 FP (Canela ×2)** → protocolo
STOP R-T1 activado; el fix general + caso al fixture envenenado entran con
las palancas ANTES de cualquier re-corrida.

## Diagnóstico de las 64 no-confirmadas (8 clasificadores + síntesis)
| Palanca | N | % |
|---|---|---|
| **Bloqueo de autoridad/gate en municipios nuevos** | 25 | 39% |
| Render interactivo (shells atende y similares) | 17 | 27% |
| Citas del modelo (2 sub-bugs: quote_ambiguous + charset iso8859) | 12 | 19% |
| Transporte (SSL CA intermedia + Content-Type ausente) | 4 | 6% |
| Legítimamente difícil (revisar correcto) | 3 | 5% |
| Patrón de plataforma / drift de URL | 3 | 4% |

**HALLAZGO MAYOR (39%)**: en 25 unidades A certificó con citas verificadas
byte-exacto y contenido vivo confirmado por curl, B sostuvo… y el resultado
final cayó a revisar. Causa raíz: el gate estructural exige
authority/identity='confirmada', y el registro de dominios solo cubre los 24
municipios del golden — los municipios nuevos en dominios custom
(pmgentil.com.br, pmvistaalegre.com.br), atende/multi24 sin entrada de
registro, quedan authority='desconocida' y el gate (correctamente fail-closed)
no publica. NO es bug del adjudicador: es COBERTURA DE AUTORIDAD, exactamente
lo que F3.P2 (registro IBGE) + reglas de autoridad por plataforma resuelven.

**Además, contra la hipótesis inicial**: el drift de URL del descubrimiento V1
solo explica ~3% aquí — las URLs de V1 eran mayormente correctas.

## Proyección honesta hacia el 80% (síntesis, conversión 70-85% por palanca)
- Solo palancas F3 originales (patrones+render+modelo): techo ~53% — INSUFICIENTE.
- Con cobertura de autoridad (la palanca nueva #1) + render + citas + transporte:
  **73-82%, y si autoridad convierte ~95% (evidencia ya verificada byte-exacto): ~83-85%** ✓
## Orden de ROI resultante
1. Cobertura de autoridad para municipios nuevos (registro IBGE + reglas de
   plataforma delegada tipo {slug}.atende.net/multi24h) — 25u, fix de
   datos+reglas generales.
2. Render interactivo F3.P5 — 17u, una mejora cubre ~12 (mismo shell atende).
3. Sub-bugs de citas: desambiguación quote_ambiguous + charset iso8859 — 12u.
4. Transporte: cadena CA (AIA) + sniffing de body sin Content-Type — 4u.
5. Tier 1.5/patrones y re-descubrimiento — 3u (menor de lo previsto).

## Estado del paso
F2.P6 EJECUTADO con resultado mixto: **precisión en territorio nuevo
excelente (0 FP duros)**; cobertura insuficiente por causas diagnosticadas y
accionables. Cierre formal pendiente de: (a) oráculo de Luis sobre Canela,
(b) implementación de las palancas 1-4 y re-corrida del holdout.
