# Concursos Tracker — Plan maestro (handoff para Codex)

> Documento autocontenido. Estado: 2026-06-23. Piloto: **solo Rio Grande do Sul (RS)**.
> Entorno: Windows 11 + Python embeddable 3.12. Sin openpyxl (usar `excel_utils.read_xlsx_dicts`).

---

## 1. Qué estamos construyendo (y por qué)

El objetivo final **no es un scraper**, es el **motor de datos de una app web de matching para concurseros**
(personas que siguen carrera en concursos públicos de Brasil).

El usuario se registra con su perfil:
- escolaridade (fundamental / médio / técnico / superior)
- profesión
- ciudad de origen + **radio de km** dispuesto a viajar **o** disposición a mudarse
- salário mínimo aceptable

La app hace dos cosas:
1. **Match** → muestra concursos/PSS **elegibles** para ese perfil.
2. **Alertas de ciclo de vida** → "salió el gabarito", "quedaste en posición X", "fuiste nomeado".

Cubrimos **concursos públicos Y processos seletivos** de prefeituras — todo lo público.

**Esto define qué datos importan** y por qué la arquitectura es como es (ver §3).

---

## 2. Estado actual (lo que ya funciona)

| Fase | Qué hace | Estado | Salida |
|---|---|---|---|
| 0 Setup | Estructura, catálogo 59 fuentes, schema | ✅ | `sources_catalog.seed.csv` |
| 1 Sonda fetch | Clasificación easy/js/hostile, fetch ladder | ✅ | `sources_catalog_phase1.csv` |
| 2A Bancas | Resolver filas con banca conocida vía índice oficial | ✅ | `ache_rs_official_pipeline_ajustado.csv` (165 concursos) |
| 2B v2 Prefeitura | Construct-and-verify `.rs.gov.br` + route probes | ✅ | `ache_rs_fase2_v2.csv` |
| 2C Registro municipal | Escaneo de los 497 municipios RS (IBGE) | ✅ | `sites_municipios_rs.csv` |
| 3A Scanner índices | Extrae documentos/eventos de cada ruta | 🔄 iniciada | `fase3a_documentos_municipais_rs.csv` |

**Números de 2C (la base sobre la que construimos):**
- **433 homes oficiales** verificados (status 200, prefeitura real)
- **306 `concursos_url`** descubiertas (route probes)
- **163 `processos_seletivos_url`** descubiertas

**Regla dura (innegociable):** un `portal_radar` (incluido Ache) puede **crear** un candidato pero
**NO verificarlo**. Un concurso sube a `official_found`/`pdf_found` solo con evidencia en dominio
oficial (`.gov.br`, banca conocida, o diário). **No inventar datos**: campo ausente en la fuente = `null`.

---

## 3. Modelo de datos: Concurso (madre) + Evento (hijo)

Son **dos entidades, no una**:

| Entidad | Campos clave | Para qué | Fuente |
|---|---|---|---|
| **Concurso** (madre) | órgão, município, banca, edital_num, tipo, cargos, vagas, salário, escolaridade, datas | **MATCHING** | Edital de abertura |
| **Evento** (hijo) | concurso_id, tipo, fecha, título, doc_url, hash, first_seen | **ALERTAS** / timeline | Banca + Diário + site |

- Un Concurso tiene **N Eventos**.
- Dedup de eventos por `município + edital_num + tipo_evento`.
- Identidad del Concurso: `esfera:orgao_sigla:edital_num` (no fuzzy sobre el nombre).
- Retificações / prorrogações = **UPDATE** del concurso existente, NO un registro nuevo.

---

## 4. Modelo de fuentes: matriz fuente × evento (doble capa por ROL)

**Insight central:** ninguna fuente sola cubre el ciclo completo. Por eso asignamos **cada evento
a su autoridad**, no elegimos una fuente única.

- Las **bancas** cubren el "durante" del certame (abertura → resultado).
- El **Diário / prefeitura** cubre el "después" administrativo (homologação → nomeação).
- Confirmado con datos reales (fila Sinimbu en el crawl 2D): muchos sites de prefeitura solo
  publican **convocações**, no el edital ni sus etapas. Eso es un problema **estructural de cobertura
  de la fuente**, no un bug del crawler. La solución es la doble capa, no perfeccionar un solo crawler.

| Evento | Autoridad principal | Respaldo |
|---|---|---|
| Edital de abertura | Banca (si tiene) | Diário / site prefeitura |
| Retificação | Banca | Diário |
| Cronograma / inscripción | Banca | — |
| Gabarito | Banca | — |
| Resultado / classificados | Banca | Diário |
| Homologação | Diário | Banca |
| **Convocação / Nomeação / Posse** | **Diário + site prefeitura** | (nunca la banca) |

### Las dos capas de descubrimiento

- **Capa 0 — Ache (radar).** Cosecha Ache RS → CSV semilla. Descubre los concursos populares.
  No es fuente final (ver Regla dura).
- **Capa 1 — Diário Municipal FAMURS (ancha).** `diariomunicipal.com.br/famurs/pesquisar` cubre
  la **cola larga** (~270 municipios sin ruta propia) + **nomeações de casi todos**. Búsqueda por
  término + fecha. **UNA integración paginada**, no 400 sites con 400 latencias.
  - ⚠️ **Excepción:** ~10-15 municipios grandes (Porto Alegre, Caxias do Sul, Pelotas, Canoas,
    Santa Maria) tienen DOM propio y **NO** usan FAMURS → tratar aparte en Capa 2.
- **Capa 2 — Sites propios (profundidad).** Bancas (edital + etapas) + los ~15 municipios-capital
  con DOM propio. **NO** mapear prefeituras 1:1 a mano.

---

## 5. Roadmap por fases (de aquí en adelante)

### 🔄 Fase 3A — Scanner de índices (en curso)
Entra a cada `concursos_url` / `processos_seletivos_url`, extrae **candidatos de documentos**:
edital, retificação, convocação, resultado, homologação, gabarito. **No descarga PDFs ni publica.**
Indexa: título, tipo, url, scores (edital_nums, dates, signals), links de descarga, hash, first_seen.
- **Próximo:** aplicarlo a los **306 + 163 ≈ 470 índices** completos → ver qué emerge.
- La granularidad de salida ya **no es "municipio" sino "documento/evento"**.

### 🔄 Fase 3B — Diário Municipal (cola larga + nomeações)
Adapter del Diário Municipal FAMURS para municipios sin ruta clara y para capturar nomeações.
Búsqueda por fecha/términos ("concurso", "processo seletivo", "edital", "convocação").
Para los ~15 municipios con DOM propio: adapter dedicado por site.
(Querido Diário queda como respaldo donde FAMURS no llegue; requiere mapping IBGE → territorio_id.)

### Fase 3C — Descarga PDFs + hash
Filtrar candidatos con score ≥ umbral y `.pdf`; descargar (requests/curl_cffi/Playwright según dominio);
SHA256 para dedup; guardar en `data/pdfs/YYYY-MM/`; registrar en `pdf_inventory.csv`.

### Fase 4 — Extracción texto/tablas
PyMuPDF (texto) + pdfplumber (tablas). Detectar PDF escaneado → OCR tesseract solo si hace falta.

### Fase 5 — Clasificación + regex de campos
Gate para publicar un concurso: (a) "edital"/"processo seletivo" en el nombre, (b) fuente oficial,
(c) inscrição futura o pasada < 60 días. Regex de `config/regex_patterns.yaml` para órgão, município,
banca, vagas (incl. CR + cotas AC/PcD), salário, taxa, período de inscrições, data das provas, escolaridade.

### Fases 6–11 (resumen)
6 Adaptadores (DOU INLABS XML, DOE-RS Playwright, Querido Diário) · 7 Crawler inteligente (link → PDF final)
· 8 IA opcional (Qwen local / RunPod, solo campos que regex no saca; confidence < 0.7 → null) ·
9 Dedup + `concursos_master.csv`/`.json` · 10 Medición de cobertura (ground truth, recall/precision/TTD)
· 11 Producción (cron nocturno).

---

## 6. Decisiones técnicas (para no tropezar)

- **Separar descubrimiento de servicio.** El crawling/scan corre en **cron nocturno** y escribe a CSV/DB.
  La app **lee de la DB**, nunca scrapea en vivo. Así los timeouts no afectan al usuario.
- **Incremental por hash.** Cada documento/página guarda hash + first_seen; solo se reprocesa lo que cambió.
- **Time-box + ThreadPool acotado** en los fetch; un solo Diário paginado vence a 400 latencias.
- **Fetch ladder:** requests → curl_cffi → Playwright (`scripts/fase1_v1.py`).
- **Señales por PATH de URL, no por contexto.** La discriminación concurso/ruido se hace sobre el
  **path** (`/concurso`, `/processo-seletivo`, `/pss`, edital_num en la URL), no sobre el blob de texto
  del CMS (evita falsos positivos tipo `/acessibilidade`).
- **Latencia honesta:** Colab manual = ~24h, no 3h. Para 3h se necesita cron en VM.

---

## 7. Archivos clave

**Datos:** `data/ache_rs_official_pipeline_ajustado.csv` (semilla 165) ·
`data/ache_rs_fase2_v2.csv` (2B v2) · `data/sites_municipios_rs.csv` (2C: 497 municipios) ·
`data/fase3a_documentos_municipais_rs.csv` (3A documentos).

**Scripts:** `scripts/ache_rs_official_pipeline.py` · `scripts/fase2_v2.py` (resolver gaps) ·
`scripts/fase2c_sites_municipios.py` (registro municipal) · `scripts/fase3a_scan_indices.py` (scanner) ·
`scripts/fase1_v1.py` (fetch ladder) · `scripts/excel_utils.py` (lector xlsx propio).

**Referencia completa:** `concurso-publico-brasil-engine/skills/.../references/development_roadmap.md`.
