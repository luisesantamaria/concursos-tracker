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
Las 4 dudas:
- **Canela CP+PSS**: portal de búsqueda oficial del municipio, estructura del
  bucket presente, pero CERO ítems en todos los años y la cita de bucket ancla
  en el MENSAJE DE AUSENCIA ("Não foram encontrados..."). El "combinado" con
  confianza alta viola el espíritu del check de doble evidencia. → PENDIENTE
  DE ORÁCULO (Luis): ¿índice oficial vacío = confirmable o revisar?
- **Montenegro/CP**: URL y decisión correctas (ítems reales verificados en
  vivo), pero la cita de bucket ancló en menú y no en ítem → nota de calidad
  de cita, no FP.
- **Itacurubi/PSS**: contenido anclado real, pero usó el contenedor genérico
  "Editais Diversos" cuando el hermano CP ya usa /site/concursos específica →
  mejorar URL, no FP.
Precisión estricta: 17/21 (81%) · precisión sin-FP-duro: 21/21 con 2-4 notas.

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
