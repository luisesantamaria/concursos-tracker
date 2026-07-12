# Runbook — Corridas locales (scraping desde Brasil)

Guía fija para correr la **fase 2** (descubrimiento de índices de concursos / processos
seletivos) desde un entorno **local en Brasil**, y traer los resultados de vuelta a la
conversación principal (donde se hacen los commits).

## Por qué local

Muchos sitios `*.rs.gov.br` **geo-bloquean** tráfico fuera de Brasil (AWS ELB:
`"Blocked request this country"`), y otros usan **Cloudflare** o **rate-limit (429)**.
Desde un servidor fuera de Brasil eso producía ~26% de "sin resultado" falsos.
Corriendo desde una **IP brasileña** (tu PC en Brasil, o una VM en `sa-east-1`/São Paulo),
el geo-block desaparece y la cobertura sube a ~83% confirmado pleno con 0 falsos positivos.

## Reparto de roles

- **Conversación principal (Claude Code web / este repo):** es el *cerebro*. Aquí se
  editan los scripts, se hacen los commits y se razona el pipeline.
- **Entorno local (Brasil):** es el *scraper*. Aquí se ejecutan las corridas pesadas
  (Tiers 0–4 con Gemini + Playwright) sin bloqueos geográficos.

## Regla de oro: correr SIEMPRE sobre la versión corregida

Antes de cualquier corrida local, **sincroniza el código**. Nunca corras sobre una copia
vieja: los fixes viven en git.

```bash
git fetch origin
git checkout claude/skill-files-accuracy-vd6uyt
git pull origin claude/skill-files-accuracy-vd6uyt
```

Y al revés: cualquier cambio de código se hace y se **commitea/pushea desde la
conversación principal**. El entorno local solo consume; no diverge.

## Setup (una sola vez)

```bash
git clone https://github.com/luisesantamaria/concursos-tracker.git
cd concursos-tracker
git checkout claude/skill-files-accuracy-vd6uyt
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
export GEMINI_API_KEY=tu_llave                       # PowerShell: $env:GEMINI_API_KEY="..."
```

## Cómo correr (por letras, con append, sin re-gastar Gemini)

El CSV de salida es **acumulativo**. Se procesan municipios por letra inicial y se van
**agregando** a `data/fase2/municipios_rs.csv` sin perder lo anterior.

```bash
# Primera tanda: A y B
python scripts/fase2_municipios/cascade_municipios.py \
  --all --letras ab --append \
  --output data/fase2/municipios_rs.csv

# Siguientes tandas: agrega C y D SIN re-correr A y B ya confirmados
python scripts/fase2_municipios/cascade_municipios.py \
  --all --letras cd --append --skip-existing \
  --output data/fase2/municipios_rs.csv
```

### Qué hace cada flag

| Flag | Efecto |
|------|--------|
| `--all` | Carga los 497 municipios de RS (fuente TCE). |
| `--letras ab` | Solo procesa los que empiezan con esas letras (insensible a acentos). |
| `--append` | Fusiona en el CSV existente: reemplaza el mismo municipio, agrega los nuevos. No borra lo anterior. |
| `--skip-existing` | **Ahorra Gemini.** Salta municipios ya **confirmados** en el CSV (no los re-procesa). Los que quedaron *sin resultado* o *revisar* **sí** se reintentan (para que un fix de código les dé otra oportunidad). Fuerza append. |
| `--no-playwright` | Salta Tier 4 (más rápido/barato, menos cobertura). |
| `--municipio "X"` | Corre un solo municipio (útil para re-probar un caso puntual). |

### Estrategia recomendada de costo

1. Corre por letras en tandas (`ab`, luego `cd`, etc.), siempre con `--append`.
2. A partir de la segunda tanda, añade `--skip-existing` para no volver a pagar Gemini
   por lo ya confirmado.
3. Para reintentar misses después de un fix de código: vuelve a correr esas letras con
   `--skip-existing` (los confirmados se saltan, solo se reintentan los pendientes).

## Qué traer de vuelta a la conversación principal

Después de cada corrida, pega **estos tres bloques** en la conversación principal:

```
### CORRIDA LOCAL — letras: <xx> — fecha: <YYYY-MM-DD>

1) SUMMARY (verbatim de la consola):
<pega el bloque "Summary: ... confirmado/probable/revisar">

2) GOLDEN SET (si se corrió el evaluador):
<pega la salida de medir_golden_set.py>

3) SIN RESULTADO / REVISAR (diagnóstico):
<lista de municipios sin URL o en revisar, y para cada uno si fue
 bloqueo de red (403/429/SSL/Cloudflare/geo) o miss real del pipeline>
```

Con eso, desde la conversación principal se diagnostica, se corrige el código, se
commitea, y la siguiente corrida local ya usa la versión arreglada.

## Tres modos de corrida (cuándo congelar y cuándo re-correr)

`--skip-existing` congela **solo los confirmados**; los `revisar`/`sin resultado`
siempre se reintentan. Eso es lo correcto para avanzar barato, pero no detecta si un
cambio de código rompió un confirmado que ya estaba fuera del golden set. Por eso se
usan tres modos según el objetivo:

| Objetivo | Comando | Costo |
|----------|---------|-------|
| Avanzar / iterar misses (día a día) | `--all --letras X --append --skip-existing` | Bajo |
| Gate de regresión (tras CADA cambio de lógica) | `--golden --output /tmp/check.csv` (sin skip) + evaluador | ~24 munis |
| Regresión total (antes de un hito) | `--all --letras X --append` (sin skip-existing) | Alto |

Política:
1. Trabajo normal → `--skip-existing` (congela confirmados, reintenta misses).
2. Después de tocar lógica de verify/select/tier → re-corre el **golden set (24)** sin
   skip a un CSV temporal y pásale el evaluador. Si aguanta, el núcleo no se rompió.
3. Antes de un hito → corrida total sin `--skip-existing` para cazar regresiones fuera
   del golden.

Hueco conocido: el golden solo cubre 24 municipios, así que no protege a un confirmado
externo cuya URL "se pudra". Mitigación futura: un modo `--reverify-confirmed`
solo-determinístico (re-baja las URLs confirmadas y marca las que ya no listan, sin
llamar a Gemini).

## Auditoría / monitoreo de falsos positivos (recurrente)

El golden cubre 24; para auditar TODOS los confirmados (cientos) y cazar FPs
escondidos de cualquier clase (URL muerta, PDF, página de detalle/noticia, tipo
equivocado) hay un auditor determinístico (sin Gemini). Córrelo desde Brasil
(necesita alcanzar los sitios), tras cada corrida grande y de forma periódica
(detecta también link rot cuando los sitios cambian):

```bash
python scripts/eval/audit_fase2_rs.py \
  --input data/fase2/municipios_rs.csv --detalle
```

Clasifica cada URL confirmada en **OK / SOFT / HARD** y escribe un CSV
`<input>_auditoria.csv` solo con los sospechosos:

- **HARD** = problema estructural casi seguro (PDF, 4xx muerta, ruta de detalle/
  noticia, o sin keyword del bucket en página con texto sustancial). Revisar y,
  si es FP, bajar a `revisar` en el CSV.
- **SOFT** = no verificable sin navegador (SPA/antibot, 5xx transitorio, listado
  corto en página JS). Ojeo opcional.

Lo que el auditor NO atrapa: ambigüedad semántica (listado real, keywords
correctas, tipo legal equivocado — ej. "Processo Seletivo Público" que es un
concurso). Ese residuo (~0.4%) necesita una muestra humana de ~40 confirmados.

## Gate de precisión (antes de escalar)

Tras una corrida, valida contra el golden set (verdad de campo, 24 municipios):

```bash
python scripts/eval/medir_golden_set.py \
  --golden authority_first/data/golden_set_v1.csv \
  --pipeline data/fase2/municipios_rs.csv --detalle
```

Reglas que no se negocian: **precisión sobre cobertura**, **cero falsos positivos**,
**no inventar URLs**, **no usar scorers numéricos** para elegir entre candidatos. Un
municipio sin fuente alcanzable se marca `revisar`/`nao_encontrado`, nunca se rellena.

## Persistencia del CSV acumulado

`data/fase2/` está en `.gitignore` (regla: no commitear outputs generados). Si quieres
que el CSV acumulado sobreviva entre máquinas/sesiones para seguir haciendo `--append`,
commitéalo explícitamente como excepción puntual desde la conversación principal:

```bash
git add -f data/fase2/municipios_rs.csv
git commit -m "Snapshot resultados fase 2 (letras <...>)"
```
