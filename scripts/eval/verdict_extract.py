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
    if s and ("Ã" in s or "Â" in s):
        try:
            s = s.encode("latin1").decode("utf-8")
        except Exception:
            pass
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


# Contexto LOCAL de un item: la linea/fila donde aparece la cita. Las ventanas
# amplias (+/-160 chars) se contaminan en listados densos; incluir filas vecinas
# tambien rompe tablas IPM, donde el binding de otra fila puede secuestrar el item.
_DATE_LINE = re.compile(r"^\s*\d{1,2}\s*(?:/|-)\s*\d{1,2}\s*(?:/|-)\s*20[12]\d\s*$")
_META_LINE = re.compile(
    r"^\s*(?:publicado\s+em\b|"
    r"(?:tipo|categoria|modalidade|situa[cç][aã]o|status)\s*:)",
    re.I,
)


def _item_scope(text: str, cita: str) -> str:
    qc = qn(cita)
    if not qc:
        return ""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if qc in qn(line):
            block = line
            # Cards de noticia/listado suelen renderizar FECHA en la linea anterior
            # y titulo en la siguiente (Canoas P). Solo anexamos si la linea previa
            # es una fecha aislada, nunca una fila de otro certame.
            if i > 0 and _DATE_LINE.match(lines[i - 1] or ""):
                block = lines[i - 1] + "\n" + block
            for nxt in lines[i + 1:i + 4]:
                if _META_LINE.match(qn(nxt)):
                    block += "\n" + nxt
            return qn(block)
    return qc


# Número de edital NN/AAAA o NN-AAAA (con lookbehind para no morder "Lei 13.019/2014").
_NUM = re.compile(r"(?<![\d./])(\d{1,4})\s*[/\-]\s*(20[12]\d)\b")
_NUM_SHORT_YEAR = re.compile(
    r"(?:edital|concursos?|processos?\s+seletiv\w*|sele[cç][aã]o|"
    r"\bpss\b|\bn(?:[\u00ba\u00b0]|o)?\b)\D{0,24}?"
    r"(?<![\d./])(\d{1,4})\s*[/\-]\s*(\d{2})\b",
    re.I,
)


def _year2(yy: str) -> str:
    y = int(yy)
    return str(2000 + y if y <= 39 else 1900 + y)


def _num_key(*texts: str) -> tuple[str, str] | None:
    for t in texts:
        m = _NUM.search(t or "")
        if m:
            return (m.group(1).lstrip("0") or "0", m.group(2))
        m2 = _NUM_SHORT_YEAR.search(t or "")
        if m2:
            return (m2.group(1).lstrip("0") or "0", _year2(m2.group(2)))
    return None
# Documentos de ciclo de vida de UN certame (no suman certame nuevo). SOLO estos —
# NO "abertura"/"inscrições"/"anexo": una ABERTURA sí crea certame (regla de Fable).
_BINDING = re.compile(
    r"(concursos?\s+p[u\u00fa]blic\w*|processos?\s+seletiv\w*|"
    r"sele[\u00e7c][a\u00e3]o\s+p[u\u00fa]blic\w*|concursos?|"
    r"processos?\s+seletiv\w*|\bpss\b)(?:"
    r"(?:\s+(?:simplificad\w*|p[u\u00fa]blic\w*|public\w*|municipal|"
    r"de\s+estagi[a\u00e1]ri\w*)){0,4}\s*n?[\u00ba\u00b0o]?\s*|"
    r"[^\n]{0,80}?edital\s*n?[\u00ba\u00b0o]?\s*)"
    r"(?<![\d./])"
    r"(\d{1,4})\s*[/\-]\s*(20[12]\d)\b",
    re.I,
)
_CYCLE = re.compile(
    r"homologa|convoca|retifica|classifica[çc]|resultad|prorroga|adiamento|errata", re.I)
_CHILD_DOC = re.compile(r"nomea[çc]|aprovad|portaria", re.I)
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
# Concursos: "concurso público", o "concurso" seguido de número/año (Concurso 2020,
# Concurso 01/2024) — pero NO "concurso de soberanas/rainha" (cultural, otro tipo).
_KW = {
    "concursos": re.compile(
        r"concursos?\s+p[uú]blic|concursos?\s+n?[º°o]?\s*\d|concursos?\s+20[12]\d", re.I),
    "processos": re.compile(r"processos?\s+seletiv|sele[çc][aã]o\s+p[uú]blic|"
                            r"sele[çc][aã]o\s+simplificad|\bpss\b|"
                            r"contrata[çc][aã]o\s+tempor", re.I),
}
_CULTURAL = re.compile(r"soberan|rainha|garota|majestade|realeza|rei\s+e\s+rainha", re.I)
_LISTING_TABLE = re.compile(
    r"\b(?:n[ºo]|numero|nro)\s*/\s*ano\s+modalidade\s+objeto\s+data\s+"
    r"(?:da\s+)?(?:disputa|publicacao)\s+detalhes\b|"
    r"titulo\s+do\s+edital\s+data\s+de\s+publicacao\s+data\s+para\s+inscricao\s+status",
    re.I,
)
_CERTAME_DOC_TABLE = re.compile(
    r"(?:concursos?\s+public\w*|processos?\s+seletiv\w*|sele[cç][aã]o\s+public\w*|"
    r"\bpss\b)[^\n]{0,60}?(?<![\d./])\d{1,4}\s*/\s*20[12]\d\s+atividade\s+data\s+edital",
    re.I,
)
_DETAIL_TARGET = re.compile(r"/concurso/detalhe/|/edital/", re.I)


def _has_listing_shell(text: str, anchors: list | None) -> bool:
    """Estructura fuerte de pagina-indice aunque hoy liste un solo certame.

    No es un scorer: exige tabla de listado con link(s) a detalle, o una tabla
    documental interna de un certame. No confirma: solo etiqueta revision barata.
    """
    blob = qn(text or "")
    if _CERTAME_DOC_TABLE.search(blob):
        return True
    if not _LISTING_TABLE.search(blob):
        return False
    for a in anchors or []:
        href = str(a.get("href", "")) if isinstance(a, dict) else ""
        label = qn(str(a.get("text", ""))) if isinstance(a, dict) else ""
        if _DETAIL_TARGET.search(href) or label in {"detalhes", "ver detalhes"}:
            return True
    return False


def _listing_declares_bucket(title: str, bucket: str, other: str, title_declares: bool) -> bool:
    """Declaracion de bucket suficiente SOLO para paginas con listing-shell fuerte."""
    if title_declares:
        return True
    title_q = qn(title or "")
    if bucket == "concursos":
        return (
            bool(re.search(r"\bconcursos?\b", title_q))
            and not _KW[other].search(title_q)
            and not _CULTURAL.search(title_q)
        )
    return False

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
               anchors: list | None = None, title: str = "") -> tuple[str, dict]:
    """Decide 'confirmar'|'revisar' sobre la evidencia extraída. Devuelve
    (decision, evidencia) con la evidencia estructurada para telemetría/muestreo."""
    low = qn(text)
    bnorm = bucket if bucket in _KW else ("concursos" if bucket == "C" else "processos")
    other = "processos" if bnorm == "concursos" else "concursos"
    kw = _KW[bnorm]
    # Título/H1 de la página como señal de tipo por defecto: un item de listado que
    # no repite la keyword del tipo (ej. "Edital de abertura das inscrições 02/2024",
    # São Marcos) hereda el tipo declarado por el título SOLO si el título declara
    # exactamente un tipo (XOR) -- una página combinada ("Concursos e Processos
    # Seletivos") no dispara el fallback, para no reabrir la puerta a items neutros
    # en páginas mixtas donde SÍ importa clasificar por contenido.
    title_q = qn(title or "")
    title_here = bool(_KW[bnorm].search(title_q))
    title_other = bool(_KW[other].search(title_q))
    title_declares = title_here and not title_other
    certames: set = set()
    n_ajeno = n_verif = n_cycle = n_offtype = 0
    item_here = item_other = 0
    for it in items or []:
        cita = it.get("cita", "")
        if not cita:
            continue
        qc = qn(cita)
        if not qc or qc not in low:
            continue
        scope = _item_scope(text, cita)
        if _emissor_ajeno(it.get("emissor"), municipio) or _CULTURAL.search(scope):
            continue
        if _KW[bnorm].search(scope):
            item_here += 1
        if _KW[other].search(scope):
            item_other += 1
    block_piso = item_other >= 2 and item_here == 0 and not title_declares
    # PISO DETERMINISTA de alto recall: los certames con binding explícito
    # ("Concurso Público nº N/AAAA", "Processo Seletivo nº N/AAAA") se cuentan
    # directo del texto renderizado — son substrings por definición (ya quote-
    # verified) y NO dependen de la recall del LLM, que sub-extrae en páginas con
    # muchos items (Boa Vista tenía 5 concursos, el LLM extrajo 1). Un edital ajeno
    # reposteado NO se escribe "Processo Seletivo nº N/AAAA del município" (es
    # "Processo Seletivo do CIEE..."), así que este piso no captura los ajenos.
    if not block_piso:
        for b in _BINDING.finditer(text or ""):
            btipo = "concursos" if re.search(r"concurso", b.group(1), re.I) else "processos"
            if btipo != bnorm:
                continue
            raw_w = (text or "")[max(0, b.start() - 120): b.end() + 120]
            w = qn(raw_w)
            if _CULTURAL.search(w) or _FOREIGN.search(raw_w):
                continue                       # cultural u emisor ajeno nombrado cerca
            if (bnorm == "concursos" and "public" not in qn(b.group(1))
                    and _KW[other].search(w)):
                continue                       # "Concurso" generico dentro de bloque PSS
            certames.add((b.group(2).lstrip("0") or "0", b.group(3)))
    n_binding_piso = len(certames)
    n_meta_floor = 0
    if not block_piso:
        lines = (text or "").splitlines()
        for i, line in enumerate(lines):
            key = _num_key(qn(line))
            if not key:
                continue
            block = line
            for nxt in lines[i + 1:i + 4]:
                if _META_LINE.match(qn(nxt)):
                    block += "\n" + nxt
            w = qn(block)
            if _BINDING.search(block) or _BINDING.search(w):
                continue
            if not kw.search(w) or _KW[other].search(w):
                continue
            if _CYCLE.search(w) or _CHILD_DOC.search(w):
                continue
            if _CULTURAL.search(w) or _FOREIGN.search(block):
                continue
            before = len(certames)
            certames.add(key)
            if len(certames) > before:
                n_meta_floor += 1
    n_piso = len(certames)
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
        scope = _item_scope(text, cita)
        if _emissor_ajeno(it.get("emissor"), municipio):
            n_ajeno += 1
            continue
        if _CULTURAL.search(scope):         # concurso cultural (soberanas) != concurso público
            n_offtype += 1
            continue
        used_title_fallback = False
        # Regla 1 — BINDING gana: el item nombra al certame padre (tipo + N/AAAA),
        # aunque el doc tenga su propio número. Colapsa docs de ciclo numerados.
        b = _BINDING.search(cita) or _BINDING.search(scope)
        if b:
            btipo = "concursos" if re.search(r"concurso", b.group(1), re.I) else "processos"
            if btipo == bnorm:
                certames.add((b.group(2).lstrip("0") or "0", b.group(3)))
            continue
        if not kw.search(scope):           # tipo del bucket por cita/bloque local
            # fallback: título declara el tipo sin ambigüedad y el item no tiene
            # marca del OTRO tipo ni cultural en su ventana local.
            if not (title_declares and not _KW[other].search(scope) and not _CULTURAL.search(scope)):
                n_offtype += 1
                continue
            used_title_fallback = True
        # Regla 2 — edital con número propio -> crea certame. Si era documento de
        # ciclo de un certame padre, la Regla 1 ya lo colapso por binding.
        key = _num_key(cita, scope)
        if key:
            certames.add(key)
        else:
            if used_title_fallback:
                n_offtype += 1
                continue
            # Regla 3 — keyword de ciclo sin número/binding -> doc huérfano.
            if _CYCLE.search(scope):
                n_cycle += 1
                continue
            # Regla 4 — keyword + año sin número -> crea certame por año.
            y = re.search(r"\b(20[12]\d)\b", scope)
            if y:
                certames.add(("Y", y.group(1)))
    ev = {
        "n_certames": len(certames),
        "certames": sorted(certames)[:8],
        "verif": n_verif, "off_type": n_offtype,
        "ciclo": n_cycle, "ajenos": n_ajeno, "piso": n_piso,
        "binding_piso": n_binding_piso, "meta_floor": n_meta_floor,
        "piso_blocked": block_piso, "item_here": item_here,
        "item_other": item_other,
        "title_declares": title_declares,
        "listing_shell": _has_listing_shell(text, anchors),
    }
    ev["listing_declares"] = _listing_declares_bucket(title, bnorm, other, title_declares)
    if len(certames) >= 2:
        return "confirmar", ev
    if len(certames) == 1 and ev["listing_shell"]:
        ev["estado"] = "revisar_certame_unico"
        ev["motivo"] = "indice con estructura de listado pero un solo certame"
        return "revisar", ev
    if len(certames) == 1:
        ev["estado"] = "revisar"
        ev["motivo"] = "un solo certame (posible detalle)"
        return "revisar", ev
    if n_ajeno and not certames:
        ev["estado"] = "revisar"
        ev["motivo"] = "solo editais de emisor ajeno"
        return "revisar", ev
    ev["estado"] = "revisar"
    ev["motivo"] = "0 certames del tipo alvo"
    return "revisar", ev
