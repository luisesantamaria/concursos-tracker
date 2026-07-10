# Handoff local — run497 chunks 5 y 6

Este documento es para ejecutar desde Brasil. No ejecutar scraping desde el
entorno web/sandbox: los sitios `*.rs.gov.br` pueden aplicar geo-bloqueo,
Cloudflare o rate limits.

## (a) Prerrequisito: sincronizar el fix antibot

La rama debe contener el commit `6ee759f` (fix antibot). Desde la raíz del clon
local, antes de cada corrida:

```bash
git fetch origin
git checkout claude/skill-files-accuracy-vd6uyt
git pull origin claude/skill-files-accuracy-vd6uyt
git merge-base --is-ancestor 6ee759f HEAD && echo "OK: fix antibot 6ee759f presente"
```

El último comando debe salir con código 0 antes de continuar.

El output acumulativo es `data/fase2/municipios_rs_local.csv`. Usa siempre
`--append --skip-existing`: el segundo flag salta sólo municipios con ambos
buckets `confirmado`; los parciales, `revisar` y vacíos se vuelven a intentar.

## Partición comprobada

La fuente de la partición no es un manifest versionado: son los artefactos
congelados de run497 en `/home/orion/.hermes/run497_scratch/`.

| chunk ya materializado | artefacto | rango de municipios | filas |
|---|---|---|---:|
| 1 | `run497_chunk1_authority.csv` | Aceguá – Capão da Canoa | 83 |
| 2 | `run497_chunk2_authority.csv` | Capão do Cipó – Feliz | 83 |
| 3 | `run497_chunk3_authority.csv` | Flores da Cunha – Mata | 83 |
| 4 | `run497_chunk4_authority.csv` | Mato Castelhano – Presidente Lucena | 83 |

Por continuación del mismo orden alfabético de 497 municipios, los restos son:

| chunk | rango exacto del corpus | filas | filtro CLI disponible |
|---|---|---:|---|
| 5 | Progresso – São Valentim do Sul | 83 | `pqrs` |
| 6 | São Valério do Sul – Xangri-Lá | 82 | `stuvwx` |

`--letras` filtra por inicial, no por intervalo de filas. Por ello `pqrs`
incluye también los P anteriores a Progresso y `stuvwx` los S anteriores a São
Valério do Sul. Con `--skip-existing` los plenamente confirmados se saltan; los
pendientes de esas letras se reintentan, que es el comportamiento intencional
del runbook.

## (b) Comando Chunk 5

```bash
python scripts/fase2_municipios/cascade_municipios.py \
  --all --letras pqrs --append --skip-existing \
  --output data/fase2/municipios_rs_local.csv
```

## (c) Comando Chunk 6

```bash
python scripts/fase2_municipios/cascade_municipios.py \
  --all --letras stuvwx --append --skip-existing \
  --output data/fase2/municipios_rs_local.csv
```

## (d) Evaluador golden después de cada chunk

```bash
python scripts/eval/medir_golden_set.py \
  --golden authority_first/data/golden_set_v1.csv \
  --pipeline data/fase2/municipios_rs_local.csv --detalle
```

## (e) Municipios a vigilar

### Objetivos del fix antibot

- **Barros Cassal** y **Boa Vista do Sul**: comprobar si ahora superan el
  rechazo WAF/TLS (403/406/429/503/error).

Ambos son letra B y por tanto **no se ejecutan** con los comandos de chunks 5
y 6. Esta directiva no añade una tercera corrida: para verificarlos hace falta
una orden explícita de rerun de la letra B.

### Golden sin evidencia run497 local

La fuente es `authority_first/docs/triage_cola_riesgo_fase2.md`, tabla de
líneas 238–247. Vigilar, si entra en el comando ejecutado:

- Chunk 5 (`pqrs`): **Santa Maria**, **São Leopoldo** y **São Pedro do Sul**.
- Chunk 6 (`stuvwx`): **Viamão**.
- Fuera de estos dos filtros: **Araricá** y **André da Rocha** (letra A).

### Capturas previamente bloqueadas dentro de las letras 5–6

El auditor congelado marca `bloqueo_antibot_no_verificable` para **Quevedos**,
**Relvado**, **Santana da Boa Vista**, **São Jerônimo**, **Taquari**,
**Teutônia** y **Vista Alegre do Prata** (ambos buckets en cada caso).

En el CSV actual los siete están plenamente `confirmado`, por lo que
`--skip-existing` los saltará: quedan como observación de auditoría, no como
re-scrape efectivo de esta directiva. Los bloqueos pendientes que sí se
reintentan automáticamente son los municipios no plenamente confirmados de
las letras seleccionadas; etiqueta cada resultado final como bloqueo de red o
miss real.

## (f) Bloques obligatorios para pegar de vuelta

Después de **cada** comando de chunk, pegar en la conversación principal:

```text
### CORRIDA LOCAL — letras: <pqrs o stuvwx> — fecha: <YYYY-MM-DD>

1) SUMMARY (verbatim de la consola):
<pega el bloque "Summary: ... confirmado/probable/revisar">

2) GOLDEN SET:
<pega la salida completa de medir_golden_set.py>

3) SIN RESULTADO / REVISAR (diagnóstico):
<lista de municipios sin URL o en revisar; para cada uno indicar
 bloqueo de red (403/429/SSL/Cloudflare/geo) o miss real del pipeline>
```
