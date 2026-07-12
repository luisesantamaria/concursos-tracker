# Scratch — baseline ANTES de los 31 buckets

Congelado antes de editar la lógica de decisión.

- HEAD: `180289cd9d84c1bcc83bbc61ca8e86d9a6e99e1d`
- `git status --short`: `?? authority_first/docs/triage_cola_riesgo_fase2.md`
- Camino real offline: `scripts/eval/cierre_dataset.py::_extract_verdict_from_fixture` → `scripts/eval/verdict_extract.py::adjudicate`.

Comando reproducible (ejecutado desde la raíz del repositorio):

```bash
python3 - <<'PY'
import json, sys
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / "scripts/eval"))
import cierre_dataset as cierre

ids = """arroio_do_tigre_concursos aurea_concursos boqueirao_do_leao_concursos cangucu_concursos canoas_processos canudos_do_vale_concursos canudos_do_vale_processos capao_do_leao_concursos chiapetta_concursos condor_processos eldorado_do_sul_concursos estrela_concursos flores_da_cunha_processos frederico_westphalen_concursos horizontina_concursos imbe_concursos imbe_processos itaara_concursos itapuca_concursos itapuca_processos lajeado_do_bugre_processos nova_boa_vista_concursos nova_padua_processos nova_petropolis_concursos nova_ramada_processos novo_xingu_concursos panambi_concursos pinheirinho_do_vale_processos pinto_bandeira_concursos poco_das_antas_concursos presidente_lucena_concursos""".split()
corpus = Path("/home/orion/.hermes/run497_corpus")
for ident in ids:
    data = json.loads((corpus / f"{ident}.json").read_text(encoding="utf-8"))
    confianza, motivo = cierre._extract_verdict_from_fixture(
        data["municipio"], data["bucket"], data.get("title", ""),
        data.get("text", ""), data.get("anchors") or [],
        data.get("items_llm") or [], url=data.get("url", ""),
    )
    print(f"{ident}\t{confianza}\t{motivo.get('motivo_code', '')}")
PY
```

| ID | veredicto actual ANTES | motivo estructurado |
|---|---|---|
| `arroio_do_tigre_concursos` | revisar | `certame_unico` |
| `aurea_concursos` | confirmado | `certames_suficientes` |
| `boqueirao_do_leao_concursos` | confirmado | `certames_suficientes` |
| `cangucu_concursos` | confirmado | `certames_suficientes` |
| `canoas_processos` | revisar | `noticia` |
| `canudos_do_vale_concursos` | confirmado | `certames_suficientes` |
| `canudos_do_vale_processos` | confirmado | `certames_suficientes` |
| `capao_do_leao_concursos` | confirmado | `certames_suficientes` |
| `chiapetta_concursos` | confirmado | `certames_suficientes` |
| `condor_processos` | confirmado | `certames_suficientes` |
| `eldorado_do_sul_concursos` | confirmado | `certames_suficientes` |
| `estrela_concursos` | confirmado | `certames_suficientes` |
| `flores_da_cunha_processos` | confirmado | `certames_suficientes` |
| `frederico_westphalen_concursos` | confirmado | `certames_suficientes` |
| `horizontina_concursos` | confirmado | `certames_suficientes` |
| `imbe_concursos` | confirmado | `certames_suficientes` |
| `imbe_processos` | confirmado | `certames_suficientes` |
| `itaara_concursos` | confirmado | `certames_suficientes` |
| `itapuca_concursos` | confirmado | `certames_suficientes` |
| `itapuca_processos` | confirmado | `certames_suficientes` |
| `lajeado_do_bugre_processos` | confirmado | `certames_suficientes` |
| `nova_boa_vista_concursos` | confirmado | `certames_suficientes` |
| `nova_padua_processos` | confirmado | `certames_suficientes` |
| `nova_petropolis_concursos` | revisar | `certame_unico` |
| `nova_ramada_processos` | confirmado | `certames_suficientes` |
| `novo_xingu_concursos` | confirmado | `certames_suficientes` |
| `panambi_concursos` | confirmado | `certames_suficientes` |
| `pinheirinho_do_vale_processos` | confirmado | `certames_suficientes` |
| `pinto_bandeira_concursos` | revisar | `certame_unico` |
| `poco_das_antas_concursos` | confirmado | `certames_suficientes` |
| `presidente_lucena_concursos` | confirmado | `certames_suficientes` |
