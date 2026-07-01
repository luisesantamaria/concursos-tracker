#!/usr/bin/env python3
"""Verificador por EXTRACCIÓN FALSIFICABLE (reemplazo del veredicto holístico).

Filosofía (diseñada con Luis + un modelo externo): un veredicto holístico
("¿es un índice?") siempre lo gana evidencia superficialmente plausible — que es
lo que hacen los arquetipos de FP (edital ajeno reposteado, detalle-de-un-certame
disfrazado). La salida es invertir los roles: **el LLM transcribe, el código decide.**

El LLM es solo un LOCALIZADOR DE SPANS: lista cada item copiando su CITA VERBATIM
(+ el emisor si hay una entidad nombrada). NO juzga. Luego un adjudicador
determinista:
  1. QUOTE-CHECK anti-alucinación: cada cita debe ser substring del texto
     renderizado (tras normalizar acentos/espacios). Cita que no está -> se descarta.
     Esto convierte al LLM en un transcriptor cuyo output se refuta mecánicamente:
     es la verificación independiente que un 2º LLM (mismo sesgo) no da.
  2. Re-deriva TODO del texto, no le cree los campos al LLM: numero/ano por regex
     sobre la cita; tipo/doc por keywords en ventana determinista alrededor; emisor
     por matching contra el municipio.
  3. Cuenta CERTAMES DISTINTOS del tipo del bucket (agrupa por (numero, ano); los
     documentos de ciclo homologação/convocação/retificação NO suman certame nuevo).
     >=2 certames -> índice; 1 certame + N docs -> detalle -> revisar; 0 -> revisar.
  4. EMISOR default-deny: un item solo cuenta si su emisor es (vacío + dominio
     oficial) o matchea el municipio / gramática intra-municipal. Entidad ajena
     nombrada (CIEE, consórcio, otro município) -> excluye el item (arquetipo A).

Estabilidad validada empíricamente (9/9 estable entre corridas sobre el caché):
la única libertad del LLM es QUÉ líneas cita, y esa varianza es detectable
(item aparece/desaparece, quote-check falla), no el conteo silencioso que oscilaba.
"""
from __future__ import annotations
import json
import re
import unicodedata
import urllib.request

# ---------------------------------------------------------------------------
# Normalización para quote-check: sin acentos, minúscula, espacios colapsados.
# El LLM copia "Seleção Simplificada" pero puede variar acento/espaciado; sin
# normalizar, el substring-check falla en páginas reales (bug observado).
# ---------------------------------------------------------------------------
def qn(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


# Número de edital NN/AAAA o NN-AAAA (con lookbehind para no morder "Lei 13.019/2014").
_NUM = re.compile(r"(?<![\d.])(\d{1,4})\s*[/\-]\s*(20[12]\d)\b")
# Documentos de ciclo de vida de UN certame (no suman certame nuevo).
_CYCLE = re.compile(
    r"homologa|convoca|retifica|resultad|classifica[çc]|inscri[çt]|gabarito|"
    r"prova|inscritos|prorroga|errata|adiamento|anexo", re.I)
# Entidades AJENAS conocidas (supra/extra-municipales). El default-deny no las
# necesita para ejecutar, pero acelerarlas y explicarlas ayuda a la telemetría.
_FOREIGN = re.compile(
    r"\bciee\b|\bfgtas\b|cons[oó]rcio|governo do estado|secretaria estadual|"
    r"universidade|instituto federal|\bif[a-z]{2}\b|funda[çc][aã]o getulio|\bfgv\b|"
    r"\btj[a-z]{2}\b|minist[eé]rio", re.I)
# Gramática intra-municipal genérica (emisor propio aunque no nombre el município).
_INTRA = re.compile(
    r"prefeitura|c[aâ]mara|secretaria municipal|fundo municipal|autarquia|samae|"
    r"instituto de previd|regime pr[oó]prio|conselho municipal|conselho tutelar", re.I)

# Keywords del tipo por bucket (para clasificar por CONTENIDO, no por título/URL).
_KW = {
    "concursos": re.compile(r"concurso\s+p[uú]blic", re.I),
    "processos": re.compile(r"processo\s+seletivo|sele[çc][aã]o\s+p[uú]blic|"
                            r"sele[çc][aã]o\s+simplificad|\bpss\b|"
                            r"contrata[çc][aã]o\s+tempor", re.I),
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cita": {"type": "string"},
                    "emissor": {"type": "string"},
                },
                "required": ["cita"],
            },
        }
    },
    "required": ["items"],
}

_EXTRACT_PROMPT = (
    "Você é um EXTRATOR, não um juiz. Liste CADA edital, aviso ou item de listagem "
    "que aparece no texto da página, copiando a CITA VERBATIM de cada um (no máximo "
    "120 caracteres, EXATAMENTE como está escrito, sem parafrasear). NÃO decida se a "
    "página é um índice; apenas transcreva o que existe. Para cada item, se houver uma "
    "ENTIDADE nomeada que o emite ou promove (ex.: CIEE, um consórcio, outro município, "
    "uma universidade), coloque em 'emissor'; se nenhuma entidade externa é nomeada, "
    "deixe 'emissor' vazio.\n\nTEXTO DA PÁGINA:\n{text}"
)


def extract_items(text: str, session, gemini_post, model: str,
                  timeout: int = 40) -> list[dict]:
    """Pide al LLM la lista de items (cita verbatim + emisor). Usa responseSchema +
    temperatura 0. `gemini_post` es la del cascade: (session, model, payload, timeout)
    -> dict. Maneja truncación: si el JSON viene cortado, recupera los items completos."""
    prompt = _EXTRACT_PROMPT.format(text=(text or "")[:14000])
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }
    try:
        resp = gemini_post(session, model, payload, timeout)
    except Exception:
        return []
    try:
        raw = resp["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return []
    if not raw:
        return []
    try:
        return json.loads(raw).get("items", []) or []
    except Exception:
        return _salvage_items(raw)


def _salvage_items(raw: str) -> list[dict]:
    """Recupera items de un JSON truncado: extrae cada objeto {"cita":...} completo."""
    items = []
    for m in re.finditer(r'\{\s*"cita"\s*:\s*"((?:[^"\\]|\\.)*)"'
                         r'(?:\s*,\s*"emissor"\s*:\s*"((?:[^"\\]|\\.)*)")?', raw):
        try:
            cita = json.loads('"' + m.group(1) + '"')
        except Exception:
            cita = m.group(1)
        em = m.group(2) or ""
        items.append({"cita": cita, "emissor": em})
    return items


def _emissor_ajeno(emissor: str, municipio: str) -> bool:
    """default-deny: True si hay una entidad nombrada que NO es el município ni
    intra-municipal genérica. Vacío -> no ajeno (índice normal no nombra emisor)."""
    em = (emissor or "").strip()
    if not em or em.lower() in ("null", "none", "-"):
        return False
    if qn(municipio) and qn(municipio) in qn(em):
        return False                       # nombra el município -> propio
    if _FOREIGN.search(em):
        return True                        # entidad ajena conocida
    if _INTRA.search(em):
        return False                       # órgão intra-municipal genérico
    return True                            # nombre propio no reconocido -> ajeno


def adjudicate(text: str, bucket: str, municipio: str, items: list[dict],
               anchors: list | None = None) -> tuple[str, dict]:
    """Decide 'confirmar'|'revisar' sobre la evidencia extraída. Devuelve
    (decision, evidencia) con la evidencia estructurada para telemetría/muestreo."""
    low = qn(text)
    kw = _KW.get(bucket if bucket in _KW else
                 ("concursos" if bucket == "C" else "processos"))
    certames: set = set()
    n_ajeno = 0
    n_verif = 0
    n_cycle = 0
    n_offtype = 0
    for it in items or []:
        cita = it.get("cita", "")
        if not cita:
            continue
        qc = qn(cita)
        if not qc or qc not in low:        # QUOTE-CHECK
            continue
        n_verif += 1
        i = low.find(qc)
        win = low[max(0, i - 160): i + len(qc) + 160]
        if not kw.search(win):             # tipo del bucket por ventana
            n_offtype += 1
            continue
        if _emissor_ajeno(it.get("emissor"), municipio):
            n_ajeno += 1
            continue
        m = _NUM.search(cita) or _NUM.search(win)
        is_cycle = bool(_CYCLE.search(win))
        if is_cycle:
            n_cycle += 1
            continue                       # doc de ciclo -> no suma certame nuevo
        if m:
            certames.add((m.group(1).lstrip("0") or "0", m.group(2)))
        else:
            y = re.search(r"\b(20[12]\d)\b", win)
            if y:
                certames.add(("Y", y.group(1)))   # listado por año pelado
    ev = {
        "n_certames": len(certames),
        "certames": sorted(certames)[:8],
        "verif": n_verif, "off_type": n_offtype,
        "ciclo": n_cycle, "ajenos": n_ajeno,
    }
    if len(certames) >= 2:
        return "confirmar", ev
    if len(certames) == 1:
        ev["motivo"] = "un solo certame (posible detalle)"
        return "revisar", ev
    if n_ajeno and not certames:
        ev["motivo"] = "solo editais de emisor ajeno"
        return "revisar", ev
    ev["motivo"] = "0 certames del tipo alvo"
    return "revisar", ev
