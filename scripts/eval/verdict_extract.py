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
_EDITAL_NUMBER_META = re.compile(
    r"\bnumero\s+do\s+edital\s*:?\s*(\d{1,4})\s*[/\-]\s*(20[12]\d)\b",
    re.I,
)
_NUMBER_BLOCK_BOUNDARY = re.compile(
    r"^\s*(?:fim|encerra|encerramento)\s*:|^\s*nao\s+houve\b|"
    r"^\s*carregar\s+mais\b",
    re.I,
)


def _line_index_for_cita(text: str, cita: str) -> int | None:
    qc = qn(cita)
    if not qc:
        return None
    for i, line in enumerate((text or "").splitlines()):
        if qc in qn(line):
            return i
    return None


def _item_scope(text: str, cita: str) -> str:
    qc = qn(cita)
    if not qc:
        return ""
    lines = (text or "").splitlines()
    idx = _line_index_for_cita(text, cita)
    if idx is None:
        return qc
    for i, line in enumerate(lines):
        if i == idx and qc in qn(line):
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
    r"(?<![\d./])(\d{1,4})\s*[/\-]\s*(\d{2})\b(?!\s*[/\-]\s*\d{2,4}\b)",
    re.I,
)
_DOC_OWN_NUMBER = re.compile(
    r"\b(?:edital|portaria|aviso|comunicad|nota|gabarito)\s*"
    r"(?:n\s*\.?\s*(?:[\u00ba\u00b0o]|o)?\.?\s*)?"
    r"(?:\d{1,4}\s*-\s*)?"
    r"(\d{1,4})\s*[/\-]\s*(20[12]\d)\b",
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


def _document_own_key(scope: str) -> tuple[str, str] | None:
    first = qn((scope or "").splitlines()[0] if scope else "")
    m = _DOC_OWN_NUMBER.search(first)
    if not m:
        return None
    return (m.group(1).lstrip("0") or "0", m.group(2))
# Documentos de ciclo de vida de UN certame (no suman certame nuevo). SOLO estos —
# NO "abertura"/"inscrições"/"anexo": una ABERTURA sí crea certame (regla de Fable).
_BINDING = re.compile(
    r"(concursos?\s+p[u\u00fa]blic\w*|processos?\s+seletiv\w*|"
    r"sele[\u00e7c][a\u00e3]o\s+p[u\u00fa]blic\w*|concursos?|"
    r"processos?\s+seletiv\w*|\bpss\b)(?:"
    r"(?:\s+(?:simplificad\w*|p[u\u00fa]blic\w*|public\w*|municipal|"
    r"de\s+estagi[a\u00e1]ri\w*)){0,4}\s*(?:n\s*\.?\s*(?:[\u00ba\u00b0o]|o)?\.?\s*)?|"
    r"[^\n]{0,80}?edital(?:\s+de\s+abertura)?\s*(?:n\s*\.?\s*(?:[\u00ba\u00b0o]|o)?\.?\s*)?)"
    r"(?<![\d./])"
    r"(\d{1,4})\s*[/\-]\s*(20[12]\d)\b",
    re.I,
)
_STRONG_LINE_BINDING = re.compile(
    r"\b(?P<tipo>"
    r"concursos?(?:\s*-\s*estatutari\w*|\s+public\w*(?:\s+municipal)?|\s+municipal)?|"
    r"processos?\s+seletiv\w*(?:\s+simplificad\w*|\s+public\w*)?|sele[cç]ao\s+public\w*|"
    r"sele[cç]ao\s+simplificad\w*|\bpss\b)"
    r"\s*(?:n\s*\.?\s*(?:[ÂºÂ°o]|o)?\.?\s*)?"
    r"(?<![\d./])(?P<num>\d{1,4})\s*[/\-]\s*(?P<ano>20[12]\d)\b",
    re.I,
)
_BINDING_DOC_ATTACHED = re.compile(
    r"\bedital(?:\s+de\s+abertura)?\s*"
    r"(?:n\s*\.?\s*(?:o)?\.?\s*)?"
    r"\d{1,4}\s*[/\-]\s*20[12]\d\b",
    re.I,
)
_CONCURSO_ROLE_DOC_SIGNAL = re.compile(
    r"\bconcursos?(?:\s+p[uú]blic\w*)?\s+para\s+"
    r"(?!provimento\b|cargos?\b|municipio\b|a\s+construcao\b)"
    r"[^\n]{3,80}?\bedital\b",
    re.I,
)
_CYCLE = re.compile(
    r"homologa|convoca|retifica|classifica[çc]|resultad|prorroga|adiamento|"
    r"errata|revoga|anula|suspende|cancela",
    re.I,
)
_CHILD_DOC = re.compile(
    r"chamada|convoca|nomea[çc]|homologa|resultad|classifica[çc]|"
    r"retifica|errata|aviso|comunicad|divulga[çc][aã]o\s+(?:das?\s+)?notas?|"
    r"heteroidentifica|aprovad|candidat[oa]s?\s+classificad|"
    r"contrata[çc][aã]o\s+tempor[aá]ria\s+de\s+candidat|portaria",
    re.I,
)
_TITLE_ONLY_PARENT = re.compile(
    r"\b(?:concursos?\s+p[uú]blic\w*|processos?\s+seletiv\w*|"
    r"sele[çc][aã]o\s+p[uú]blic\w*|sele[çc][aã]o\s+simplificad\w*|"
    r"\bpss\b)\b[^\n]{0,50}?\b(20[12]\d)\b",
    re.I,
)
_NEWS_ARTICLE_MARKERS = (
    "clique para ouvir esta noticia",
    "noticias relacionadas",
    "download imagem original",
    "compartilhe",
    "escritorio de comunicacao",
)
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


_NAV_TERM = re.compile(
    r"\b(?:lgpd|portal|transpar[eê]ncia|licita(?:con|[çc][oõ]es)|devolve\s+icms|"
    r"plano\s+(?:municipal|anual|plurianual)|contas\s+p[uú]blicas|legisla[çc][aã]o|"
    r"not[ií]cias|famurs|di[aá]rio\s+oficial|ouvidoria|\bsic\b|"
    r"atendimento\s+ao\s+contribuinte|matr[ií]culas|parcerias|licenciamento|"
    r"radar\s+da\s+transpar[eê]ncia|soberanas|portal\s+do\s+servidor|"
    r"\b1doc\b|\bitbi\b|\bnf\s+eletr[oô]nica|julgamento\s+de\s+contas)\b",
    re.I,
)
_LISTING_CONTEXT = re.compile(
    r"\b(?:modalidade|situa[çc][aã]o|status|in[ií]cio|fim|publicado\s+em|"
    r"objeto|data\s+de\s+publica[çc][aã]o|inscri[çc][oõ]es|n[uú]mero\s+do\s+edital)\b",
    re.I,
)
_ROW_OPENING_META = re.compile(
    r"\b(?:tipo|situa[cÃ§][aÃ£]o|status|in[iÃ­]cio|fim|inscri[cÃ§][oÃµ]es|"
    r"numero\s+do\s+edital|n[uÃº]mero\s+do\s+edital)\s*:|"
    r"\bconcurso\s+situa[cÃ§][aÃ£]o\b|"
    r"\b(?:inscri[cÃ§][oÃµ]es?\s+abertas?|em\s+andamento|encerrad[oa])\b|"
    r"\bn\s*(?:o|Âº|Â°)?\s*/\s*ano\s+modalidade\b|"
    r"\bmodalidade\s+objeto\s+data\b",
    re.I,
)
_OPENING_DOC = re.compile(
    r"\bedital\s+de\s+abertura\b|\babertura\s+(?:das?\s+)?inscricoes\b|"
    r"\babertura\s+(?:das?\s+)?inscri[cÃ§][oÃµ]es\b|\babertura\b|"
    r"\babre\s+(?:concursos?\s+public\w*|processos?\s+seletiv\w*)\b|"
    r"\btorna\s+publica\s+a\s+abertura\b|"
    r"\brealizacao\s+de\s+(?:processos?\s+seletiv\w*|sele[cÃ§][aÃ£]o\s+public\w*)\b|"
    r"\binscricoes?\s+de\s+\d{1,2}\s*/\s*\d{1,2}\s*/\s*20[12]\d\s+a\b|"
    r"\binscri[cÃ§][oÃµ]es?\s+de\s+\d{1,2}\s*/\s*\d{1,2}\s*/\s*20[12]\d\s+a\b|"
    r"\bprazo.{0,80}inscri",
    re.I,
)
_DOC_WORD = re.compile(r"\b(?:edital|portaria|aviso|comunicad|nota|gabarito)\b", re.I)
_PROCESS_NUMBER_META = re.compile(r"^\s*n(?:[ÂºÂ°o]|o)?\.?\s*processo\s*:", re.I)
_DOC_ACCESSORY_SIGNAL = re.compile(
    r"homologa|convoca|nomea[cÃ§]|resultad|classifica[cÃ§]|gabarito|"
    r"portaria|errata|retifica|revoga|anula|suspende|cancela|"
    r"nota|aviso|comunicad|prova|habilitad|"
    r"inscricoes?\s+(?:homologad|preliminar|oficial)|"
    r"inscri[cÃ§][oÃµ]es?\s+(?:homologad|preliminar|oficial)",
    re.I,
)


def _is_navigation_cluster(lines: list[str], idx: int | None) -> bool:
    """Detecta bloques de menú/sitemap: muchas líneas cortas de navegación,
    sin metadatos de item de listado cerca. Una línea "Concurso Público 2019"
    en ese bloque no debe alimentar pisos deterministas."""
    if idx is None:
        return False
    window = [qn(x) for x in lines[max(0, idx - 6): min(len(lines), idx + 7)]]
    window = [x for x in window if x]
    if not window or any(_LISTING_CONTEXT.search(x) or _DATE_LINE.match(x) for x in window):
        return False
    nav_hits = sum(1 for x in window if _NAV_TERM.search(x))
    short_lines = sum(1 for x in window if len(x) <= 90)
    return nav_hits >= 3 and short_lines >= max(5, len(window) // 2)


def _is_news_article(text: str, title: str, listing_shell: bool) -> bool:
    if listing_shell:
        return False
    q = qn(text or "")
    if not q or _LISTING_TABLE.search(q) or _CERTAME_DOC_TABLE.search(q):
        return False
    marker_hits = sum(1 for marker in _NEWS_ARTICLE_MARKERS if marker in q)
    if marker_hits < 2:
        return False
    title_q = qn(title or "")
    generic_title = {
        "concursos",
        "concurso",
        "concursos publicos",
        "concurso publico",
        "processos seletivos",
        "processo seletivo",
        "processos seletivos simplificados",
    }
    if title_q in generic_title:
        return False
    return "noticias relacionadas" in q or "clique para ouvir esta noticia" in q


def _binding_bucket(tipo: str) -> str:
    return "concursos" if "concurso" in qn(tipo) else "processos"


def _strong_line_binding(line: str) -> tuple[str, tuple[str, str]] | None:
    m = _STRONG_LINE_BINDING.search(qn(line))
    if not m:
        return None
    return (
        _binding_bucket(m.group("tipo")),
        (m.group("num").lstrip("0") or "0", m.group("ano")),
    )


def _strong_line_is_certame_heading(line: str, bucket: str) -> bool:
    q = qn(line)
    if bucket != "concursos":
        return True
    if re.search(r"\bconcursos?\s+(?:public|municipal)", q):
        return True
    return bool(re.match(
        r"\s*concursos?\s*(?:n(?:o)?\.?\s*)?\d{1,4}\s*[/\-]\s*20[12]\d\b",
        q,
        re.I,
    ))


def _edital_number_meta_key(line: str) -> tuple[str, str] | None:
    m = _EDITAL_NUMBER_META.search(qn(line))
    if not m:
        return None
    return (m.group(1).lstrip("0") or "0", m.group(2))


def _number_meta_block(lines: list[str], idx: int) -> str:
    start = max(0, idx - 14)
    for j in range(idx - 1, start - 1, -1):
        q = qn(lines[j])
        if _EDITAL_NUMBER_META.search(q) or _NUMBER_BLOCK_BOUNDARY.search(q):
            start = j + 1
            break
    return "\n".join(lines[start:idx + 1])


def _has_following_number_meta(lines: list[str], idx: int | None, year: str) -> bool:
    if idx is None:
        return False
    for j in range(idx + 1, min(len(lines), idx + 15)):
        q = qn(lines[j])
        if _NUMBER_BLOCK_BOUNDARY.search(q):
            return False
        key = _edital_number_meta_key(lines[j])
        if key and key[1] == year:
            return True
    return False


def _title_certame_key(line: str, bucket: str, other: str) -> tuple[str, str] | None:
    """Fallback for unnumbered PSS indexes that distinguish certames by role/title."""
    if bucket != "processos":
        return None
    q = qn(line)
    if not q or _num_key(q) or _KW[other].search(q):
        return None
    y = re.search(r"\b(20[12]\d)\b", q)
    if not y or not _KW[bucket].search(q):
        return None
    q = re.sub(r"^\s*\d{1,2}\s*/\s*\d{1,2}\s*/\s*20[12]\d\s*\|\s*", "", q)
    q = re.sub(r"\([^)]*\b(?:kb|mb)\b[^)]*\)\s*$", "", q).strip()
    q = re.sub(r"\.pdf\s*$", "", q).strip()
    m = re.search(
        r"processos?\s+seletiv\w*(?:\s+\w+){0,2}?\s+"
        r"(?:para\s+(?:contratacao\s+(?:de|em\s+carater\s+emergencial\s+de)\s+)?|"
        r"(?:do|para\s+o|para\s+a)?\s*cargo\s+de\s+)?"
        r"(?P<role>[a-z][a-z0-9 ]{4,80})$",
        q,
    )
    if not m:
        return None
    role = m.group("role")
    role = re.sub(r"\b(?:resultado|final|preliminar|homologacao|inscricoes|retificacao)\b", " ", role)
    role = re.sub(r"\bassistencia\s+social\b", "assistente social", role)
    role = re.sub(r"\bprofessores\b", "professor", role)
    role = re.sub(r"\s+", " ", role).strip(" -")
    if len(role) < 5:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", role).strip("-")[:48]
    if not slug:
        return None
    return ("T:" + slug, y.group(1))


def _has_title_certame_for_year(certames: set, year: str) -> bool:
    return any(
        isinstance(k, tuple) and len(k) == 2 and str(k[0]).startswith("T:") and k[1] == year
        for k in certames
    )


def _has_any_certame_for_year(certames: set, year: str) -> bool:
    return any(
        isinstance(k, tuple) and len(k) == 2 and k[1] == year
        for k in certames
    )


def _nearby_numbered_certame(lines: list[str], idx: int, bucket: str,
                             other: str, year: str) -> bool:
    for j in range(max(0, idx - 8), min(len(lines), idx + 9)):
        line = lines[j]
        q = qn(line)
        key = _num_key(q)
        if not key or key[1] != year:
            continue
        if _CULTURAL.search(q) or _FOREIGN.search(line):
            continue
        strong = _strong_line_binding(line)
        if strong and strong[0] == bucket:
            return True
        b = _BINDING.search(line)
        if b and _binding_bucket(b.group(1)) == bucket:
            return True
        if _KW[bucket].search(q) and not _KW[other].search(q):
            return True
        if re.search(r"\bedital\b", q):
            return True
    return False


def _block_declares_bucket(block: str, bucket: str, other: str) -> bool:
    q = qn(block)
    if bucket == "concursos":
        if re.search(r"\bmodalidade\s*:\s*concursos?(?:\s+public\w*)?\b", q):
            return True
    else:
        if re.search(r"\bmodalidade\s*:\s*(?:processos?\s+seletiv\w*|sele[cç]ao\s+public\w*)\b", q):
            return True
    return bool(_KW[bucket].search(q) and not _KW[other].search(q))


def _item_context_block(lines: list[str], idx: int | None, max_after: int = 10) -> str:
    if idx is None:
        return ""
    out: list[str] = []
    end = min(len(lines), idx + max_after + 1)
    for j in range(idx, end):
        q = qn(lines[j])
        if j > idx and re.match(
                r"^\s*(?:edital|concurso|processo\s+seletivo|\bpss\b)\b", q
        ) and _num_key(q):
            break
        out.append(lines[j])
    return "\n".join(out)


def _accessory_doc(scope: str, block: str = "") -> bool:
    w = qn((scope or "") + "\n" + (block or ""))
    return bool(_CYCLE.search(w) or _CHILD_DOC.search(w))


def _title_only_parent_key(line: str, bucket: str, other: str) -> tuple[str, str] | None:
    q = qn(line)
    if not q or _num_key(q) or _KW[other].search(q):
        return None
    if not _KW[bucket].search(q):
        return None
    m = _TITLE_ONLY_PARENT.search(q)
    if not m:
        return None
    return ("Y", m.group(1))


_FILTER_CATEGORY_TERM = re.compile(
    r"^\s*(?:modalidade|ver\s+todas|contrata[çc][oõ]es\s+tempor[aá]rias?|"
    r"est[aá]gio|habitacionais|portaria|processo\s+administrativo|"
    r"processo\s+de\s+sele[çc][aã]o(?:\s+para\b.*)?|"
    r"concurso\s+p[uú]blico(?:\s+20[12]\d)?|"
    r"concursos?\s+p[uú]blicos?)\s*$",
    re.I,
)


def _is_filter_category_cluster(lines: list[str], idx: int) -> bool:
    window = [qn(x) for x in lines[max(0, idx - 3): min(len(lines), idx + 8)]]
    hits = sum(1 for x in window if _FILTER_CATEGORY_TERM.match(x))
    return hits >= 4 and any(x in {"modalidade", "ver todas"} for x in window)


def _has_distinct_selection_role(scope: str, bucket: str) -> bool:
    if bucket != "processos":
        return False
    q = qn(scope)
    role_patterns = (
        r"processos?\s+seletiv\w*(?:\s+simplificad\w*)?\s+para\s+"
        r"(?!estagi|candidat|inscri|prova|manifestar\b)[a-z][a-z0-9 ]{3,}",
        r"contrato\s+por\s+prazo\s+determinado\s+para\s+"
        r"(?!candidat)[a-z][a-z0-9 ]{3,}",
        r"contrata[çc][aã]o\s+tempor[aá]ria\s+(?:de|para)\s+"
        r"(?!candidat)[a-z][a-z0-9 ]{3,}",
    )
    return any(re.search(p, q) for p in role_patterns)


def _parent_key_above(text: str, idx: int | None, bucket: str,
                      other: str) -> tuple[str, str] | None:
    if idx is None:
        return None
    lines = (text or "").splitlines()
    for j in range(idx - 1, max(-1, idx - 10), -1):
        line = lines[j]
        q = qn(line)
        if not q:
            continue
        strong = _strong_line_binding(line)
        if strong and strong[0] == bucket:
            return strong[1]
        b = _BINDING.search(line)
        if b and _binding_bucket(b.group(1)) == bucket:
            return (b.group(2).lstrip("0") or "0", b.group(3))
        key = _num_key(line)
        if key and _KW[bucket].search(q) and not _KW[other].search(q):
            return key
    return None


def _parent_title_key_above(text: str, idx: int | None, bucket: str,
                            other: str) -> tuple[str, str] | None:
    if idx is None:
        return None
    lines = (text or "").splitlines()
    for j in range(idx - 1, max(-1, idx - 80), -1):
        if _is_filter_category_cluster(lines, j):
            continue
        key = _title_only_parent_key(lines[j], bucket, other)
        if key:
            return key
    return None


def _line_context(lines: list[str], idx: int | None, before: int = 2, after: int = 6) -> str:
    if idx is None:
        return ""
    return "\n".join(lines[max(0, idx - before): min(len(lines), idx + after + 1)])


def _numbered_doc_block(lines: list[str], idx: int | None, max_after: int = 8) -> str:
    if idx is None:
        return ""
    out = [lines[idx]]
    base_key = _num_key(qn(lines[idx]))
    for j in range(idx + 1, min(len(lines), idx + max_after + 1)):
        next_key = _num_key(qn(lines[j]))
        if next_key and next_key != base_key:
            break
        out.append(lines[j])
    return "\n".join(out)


def _binding_bucket_compatible(btipo: str, bucket: str, match_text: str,
                               allow_cross_parent: bool = False) -> bool:
    if btipo == bucket:
        return True
    if bucket == "concursos" and btipo == "processos" and allow_cross_parent:
        return "estagi" not in qn(match_text)
    return False


def _binding_founds(match_text: str, context: str) -> bool:
    q = qn(match_text)
    cq = qn((context or "") + "\n" + (match_text or ""))
    doc_attached = bool(_BINDING_DOC_ATTACHED.search(q))
    role_doc = bool(
        doc_attached
        and _CONCURSO_ROLE_DOC_SIGNAL.search(q)
        and not _KW["processos"].search(cq)
    )
    if role_doc and not _DOC_ACCESSORY_SIGNAL.search(q):
        return True
    if _DOC_ACCESSORY_SIGNAL.search(cq):
        if "edital" in q:
            return False
        if "concurso" in q and not re.search(r"\bconcursos?\s+(?:public|municipal)", q):
            return False
    if doc_attached:
        return bool(
            _OPENING_DOC.search(cq)
            or _ROW_OPENING_META.search(cq)
        )
    if "edital" not in q:
        return True
    if _DOC_ACCESSORY_SIGNAL.search(cq):
        return False
    return True


def _type_bound_key(scope: str, bucket: str, other: str,
                    allow_cross_parent: bool = False) -> tuple[str, str] | None:
    doc_key = _document_own_key(scope or "")
    for b in _BINDING.finditer(scope or ""):
        btipo = _binding_bucket(b.group(1))
        if not _binding_bucket_compatible(btipo, bucket, b.group(0), allow_cross_parent):
            continue
        key = (b.group(2).lstrip("0") or "0", b.group(3))
        founds = _binding_founds(b.group(0), scope)
        if not founds:
            if doc_key and key != doc_key:
                founds = True
        if not founds:
            continue
        return key
    if bucket == "processos":
        for m in _NUM_SHORT_YEAR.finditer(qn(scope)):
            mt = m.group(0)
            if "estagi" in mt:
                continue
            if _KW[bucket].search(mt):
                return (m.group(1).lstrip("0") or "0", _year2(m.group(2)))
    return None


def _paired_type_bound_keys(scope: str, bucket: str, other: str,
                            allow_cross_parent: bool = False) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    q = qn(scope)
    pat = re.compile(
        r"(concursos?\s+p[uú]blic\w*|processos?\s+seletiv\w*|"
        r"sele[cç][aã]o\s+p[uú]blic\w*|\bpss\b)"
        r"[^\n]{0,80}?\bn(?:[º°o]|o)?\.?\s*"
        r"(\d{1,4})\s*(?:e|,)\s*(\d{1,4})\s*[/\-]\s*(20[12]\d)\b",
        re.I,
    )
    for m in pat.finditer(q):
        btipo = _binding_bucket(m.group(1))
        if not _binding_bucket_compatible(btipo, bucket, m.group(0), allow_cross_parent):
            continue
        for n in (m.group(2), m.group(3)):
            out.append((n.lstrip("0") or "0", m.group(4)))
    return out


def _context_year(scope: str) -> str | None:
    m = re.search(r"\b(20[12]\d)\b", qn(scope))
    return m.group(1) if m else None


_ROLE_BAD_TERM = re.compile(
    r"\b(?:postagem|publicado|visualizar|download|arquivo|pdf|tamanho|"
    r"paginas?|pagina|curso|cursos|qualificacao|capacitacao|programa|"
    r"servidores?|execucao|servicos?|secretaria|municipal|"
    r"https?|www|homologa|resultado|classifica|"
    r"convoca|retifica|prorroga|recurso|inscric|julgamento|preliminar|"
    r"final|de\s+\d{1,2}\s+de|jan(?:eiro)?|fev(?:ereiro)?|mar(?:co)?|"
    r"abr(?:il)?|mai(?:o)?|jun(?:ho)?|jul(?:ho)?|ago(?:sto)?|set(?:embro)?|"
    r"out(?:ubro)?|nov(?:embro)?|dez(?:embro)?)\b",
    re.I,
)
_ROLE_REAL_HINT = re.compile(
    r"\b(?:professor|operador|maquinas?|odontolog|dentista|assistente|"
    r"social|monitor|motorista|servente|operario|fiscal|visitador|"
    r"enfermeir|medic|agente|tecnico|auxiliar|psicolog|farmaceut|"
    r"nutricion|procurador|engenheir|arquitet|contador|veterinari|"
    r"merendeir|cozinheir|pedagog|fonoaudiolog|estagiari)\b",
    re.I,
)


def _role_slug_ok(role: str) -> bool:
    rq = qn(role)
    if not rq:
        return False
    if _NAV_TERM.search(rq):
        return False
    if re.search(r"\d", rq) and not _ROLE_REAL_HINT.search(rq):
        return False
    if _ROLE_BAD_TERM.search(rq) and not _ROLE_REAL_HINT.search(rq):
        return False
    return True


def _process_opening_role_ok(scope: str) -> bool:
    q = qn(scope)
    if not _KW["processos"].search(q):
        return True
    if (
        re.search(r"\bcontratacao\s+temporaria\s+de\s+servidores?\s+para\s+execucao\s+de\s+servicos\b", q)
        and not _ROLE_REAL_HINT.search(q)
    ):
        return False
    return True


def _role_certame_key(scope: str, bucket: str) -> tuple[str, str] | None:
    if bucket != "processos":
        return None
    q = qn(scope)
    if not _KW[bucket].search(q) or "estagi" in q:
        return None
    year = _context_year(q)
    if not year:
        return None
    edital_role_re = re.compile(
        r"\bedital\s*(?:n(?:o)?\.?)?\s*\d{1,4}\s*[/\-]\s*20[12]\d\s*[-–]?\s*"
        r"(?P<role>[a-z][a-z0-9 ]{4,80})",
    )
    for edital_role in edital_role_re.finditer(q):
        role = re.split(
            r"\b(?:categoria|ano|data|publicado|situacao|homologa|classifica|resultado|"
            r"convoca|inscricoes|inscri[cç][oõ]es|retifica|prorroga|recurso)\b",
            edital_role.group("role"),
        )[0]
        role = re.sub(r"[^a-z0-9 ]+", " ", role)
        role = re.sub(r"\s+", " ", role).strip()
        if len(role) >= 5 and not re.search(
                r"\b(?:candidat|inscri|prova|recurso|nota|final|preliminar|edital)\b", role
        ) and _role_slug_ok(role):
            slug = re.sub(r"[^a-z0-9]+", "-", role).strip("-")[:48]
            if slug:
                return ("T:" + slug, year)
    m = re.search(r"(?:processos?\s+seletiv\w*|\bpss\b)(?P<tail>[a-z0-9 .º°/_-]{0,120})", q)
    if not m:
        return None
    tail = m.group("tail")
    tail = re.sub(r"^\s*(?:simplificad\w*|public\w*)\b", " ", tail)
    tail = re.sub(r"\bedital\s*(?:n(?:o)?\.?)?\s*\d{1,4}\s*[/\-]?\s*(?:20[12]\d)?", " ", tail)
    tail = re.sub(r"\b(?:n(?:o)?\.?)?\s*\d{1,4}\s*[/\-]\s*20[12]\d\b", " ", tail)
    tail = re.sub(r"\b(?:para|de|do|da|dos|das|o|a|os|as)\b", " ", tail)
    tail = re.split(
        r"\b(?:categoria|ano|data|publicado|situacao|homologa|classifica|resultado|"
        r"convoca|inscricoes|inscri[cç][oõ]es|edital|ata)\b",
        tail,
    )[0]
    tail = re.sub(r"[^a-z0-9 ]+", " ", tail)
    tail = re.sub(r"\s+", " ", tail).strip()
    if len(tail) < 5:
        return None
    if re.search(r"\b(?:candidat|inscri|prova|recurso|nota|final|preliminar)\b", tail):
        return None
    if not _role_slug_ok(tail):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", tail).strip("-")[:48]
    return ("T:" + slug, year) if slug else None


def _founds_certame(line: str, context: str, bucket: str, other: str,
                    *, text: str = "", idx: int | None = None,
                    allow_cross_parent: bool = False) -> tuple[str, str] | None:
    if qn(context or "").startswith(qn(line or "")):
        scope = context or line or ""
    else:
        scope = (line or "") + "\n" + (context or "")
    q = qn(scope)
    header_q = ""
    if text and idx is not None:
        header_q = qn(_line_context((text or "").splitlines(), idx, before=3, after=0))
    type_key = _type_bound_key(scope, bucket, other, allow_cross_parent)
    if type_key:
        return type_key
    strong = _strong_line_binding(line)
    if strong and _binding_bucket_compatible(strong[0], bucket, line, allow_cross_parent):
        if _strong_line_is_certame_heading(line, bucket):
            return strong[1]
    role_key = _role_certame_key(scope, bucket)
    if role_key:
        return role_key
    key = _num_key(qn(line), q)
    if not key or _PROCESS_NUMBER_META.match(qn(line)):
        return None
    if _DOC_ACCESSORY_SIGNAL.search(q):
        if text and not _KW[bucket].search(qn(line)):
            title_parent = _parent_title_key_above(text, idx, bucket, other)
            if title_parent:
                return title_parent
        return None
    if _OPENING_DOC.search(q) or _ROW_OPENING_META.search(q) or _ROW_OPENING_META.search(header_q):
        if bucket == "processos" and not _process_opening_role_ok(q):
            return None
        return key
    return None


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


def _extract_one_window(window_text: str, session, gemini_post, model: str,
                        timeout: int, raise_errors: bool) -> list[dict]:
    prompt = _EXTRACT_PROMPT.format(text=(window_text or "")[:14000])
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
    except Exception as e:
        if raise_errors:
            raise RuntimeError(f"extract_items_failed: {e}") from e
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


def extract_items(text: str, session, gemini_post, model: str,
                  timeout: int = 40, raise_errors: bool = False) -> list[dict]:
    """Pide al LLM items sobre ventanas solapadas del texto renderizado.

    El quote-check se preserva en adjudicate(): cada cita se verifica contra el
    texto completo, no contra la ventana que la produjo.
    """
    full_text = text or ""
    if len(full_text) <= 14000:
        windows = [full_text]
    else:
        windows = [
            full_text[i:i + 14000]
            for i in range(0, min(len(full_text), 42000), 12000)
        ]
    seen: set[str] = set()
    items: list[dict] = []
    for window in windows:
        for it in _extract_one_window(
                window, session, gemini_post, model, timeout, raise_errors):
            k = qn(it.get("cita", ""))
            if k and k not in seen:
                seen.add(k)
                items.append(it)
    return items


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
    title_combo = bool(re.search(r"\bconcursos?\b", title_q)) and bool(_KW["processos"].search(title_q))
    title_declares = title_here and not title_other
    certames: set = set()
    n_ajeno = n_verif = n_cycle = n_offtype = 0
    item_here = item_other = 0
    lines = (text or "").splitlines()
    listing_shell = _has_listing_shell(text, anchors)
    if _is_news_article(text, title, listing_shell):
        ev = {
            "n_certames": 0,
            "certames": [],
            "verif": 0, "off_type": 0,
            "ciclo": 0, "ajenos": 0, "piso": 0,
            "binding_piso": 0, "meta_floor": 0,
            "strong_floor": 0, "number_meta_floor": 0,
            "title_floor": 0,
            "piso_blocked": False, "item_here": 0,
            "item_other": 0,
            "title_declares": title_declares,
            "listing_shell": listing_shell,
            "listing_declares": False,
            "estado": "revisar",
            "motivo": "nota de prensa/noticia individual, no indice",
            "motivo_code": "revisar_sem:noticia",
        }
        return "revisar", ev
    for it in items or []:
        cita = it.get("cita", "")
        if not cita:
            continue
        qc = qn(cita)
        if not qc or qc not in low:
            continue
        scope = _item_scope(text, cita)
        if _is_navigation_cluster(lines, _line_index_for_cita(text, cita)):
            continue
        if _emissor_ajeno(it.get("emissor"), municipio) or _CULTURAL.search(scope):
            continue
        if _KW[bnorm].search(scope):
            item_here += 1
        if _KW[other].search(scope):
            item_other += 1
    text_other = len(_KW[other].findall(low))
    block_piso = (
        (item_other >= 2 and item_here == 0)
        or (not listing_shell and item_here == 0 and text_other >= 2)
    ) and not title_declares
    allow_cross_parent = bnorm == "concursos" and title_declares
    n_strong_floor = 0
    allow_strong_floor = not (block_piso and title_combo and not listing_shell)
    for line_i, line in enumerate(lines):
        if not allow_strong_floor:
            break
        if _is_navigation_cluster(lines, line_i):
            continue
        strong = _strong_line_binding(line)
        if not strong:
            continue
        btipo, key = strong
        if btipo != bnorm:
            continue
        q = qn(line)
        if _CULTURAL.search(q) or _FOREIGN.search(line):
            continue
        if _KW[other].search(q) and not _KW[bnorm].search(q):
            continue
        key = _founds_certame(line, line, bnorm, other, text=text, idx=line_i)
        if not key:
            continue
        before = len(certames)
        certames.add(key)
        if len(certames) > before:
            n_strong_floor += 1
    # PISO DETERMINISTA de alto recall: los certames con binding explícito
    # ("Concurso Público nº N/AAAA", "Processo Seletivo nº N/AAAA") se cuentan
    # directo del texto renderizado — son substrings por definición (ya quote-
    # verified) y NO dependen de la recall del LLM, que sub-extrae en páginas con
    # muchos items (Boa Vista tenía 5 concursos, el LLM extrajo 1). Un edital ajeno
    # reposteado NO se escribe "Processo Seletivo nº N/AAAA del município" (es
    # "Processo Seletivo do CIEE..."), así que este piso no captura los ajenos.
    if not block_piso:
        for key in _paired_type_bound_keys(text or "", bnorm, other, allow_cross_parent):
            certames.add(key)
        for b in _BINDING.finditer(text or ""):
            btipo = _binding_bucket(b.group(1))
            if not _binding_bucket_compatible(btipo, bnorm, b.group(0), allow_cross_parent):
                continue
            line_i = (text or "").count("\n", 0, b.start())
            if _is_navigation_cluster(lines, line_i):
                continue
            raw_w = (text or "")[max(0, b.start() - 120): b.end() + 120]
            w = qn(raw_w)
            if _CULTURAL.search(w) or _FOREIGN.search(raw_w):
                continue                       # cultural u emisor ajeno nombrado cerca
            if (btipo == bnorm and bnorm == "concursos" and "public" not in qn(b.group(1))
                    and _KW[other].search(w)):
                continue                       # "Concurso" generico dentro de bloque PSS
            line = lines[line_i] if 0 <= line_i < len(lines) else b.group(0)
            key = _founds_certame(line, _numbered_doc_block(lines, line_i), bnorm, other,
                                  text=text, idx=line_i,
                                  allow_cross_parent=allow_cross_parent)
            if key:
                certames.add(key)
    n_binding_piso = len(certames)
    n_number_meta_floor = 0
    for i, line in enumerate(lines):
        key = _edital_number_meta_key(line)
        if not key:
            continue
        block = _number_meta_block(lines, i)
        w = qn(block)
        if not _block_declares_bucket(block, bnorm, other):
            continue
        if _CULTURAL.search(w) or _FOREIGN.search(block):
            continue
        key = _founds_certame(line, block, bnorm, other, text=text, idx=i,
                              allow_cross_parent=allow_cross_parent)
        if not key:
            continue
        before = len(certames)
        certames.add(key)
        if len(certames) > before:
            n_number_meta_floor += 1
    n_title_floor = 0
    for i, line in enumerate(lines):
        key = _title_certame_key(line, bnorm, other)
        if not key:
            continue
        if _nearby_numbered_certame(lines, i, bnorm, other, key[1]):
            continue
        if _CULTURAL.search(line) or _FOREIGN.search(line):
            continue
        before = len(certames)
        certames.add(key)
        if len(certames) > before:
            n_title_floor += 1
    if bnorm == "processos":
        for i, line in enumerate(lines):
            q = qn(line)
            if _is_navigation_cluster(lines, i):
                continue
            if "edital" not in q or not _KW[bnorm].search(q) or _KW[other].search(q):
                continue
            if _CULTURAL.search(q) or _FOREIGN.search(line):
                continue
            role_key = _role_certame_key(line + "\n" + _line_context(lines, i, before=2, after=4), bnorm)
            if not role_key:
                continue
            before = len(certames)
            certames.add(role_key)
            if len(certames) > before:
                n_title_floor += 1
    n_meta_floor = 0
    if not block_piso:
        for i, line in enumerate(lines):
            key = _num_key(qn(line))
            if not key:
                continue
            if _is_navigation_cluster(lines, i):
                continue
            block = _numbered_doc_block(lines, i)
            w = qn(block)
            if not kw.search(w) or _KW[other].search(w):
                continue
            if _CULTURAL.search(w) or _FOREIGN.search(block):
                continue
            key = _founds_certame(line, block, bnorm, other, text=text, idx=i,
                                  allow_cross_parent=allow_cross_parent)
            if not key:
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
        line_idx = _line_index_for_cita(text, cita)
        scope = _item_scope(text, cita)
        item_block = _item_context_block(lines, line_idx)
        type_scope = qn(scope + "\n" + item_block)
        is_accessory = _accessory_doc(scope, item_block)
        if _is_navigation_cluster(lines, line_idx):
            n_offtype += 1
            continue
        if _emissor_ajeno(it.get("emissor"), municipio):
            n_ajeno += 1
            continue
        if _CULTURAL.search(scope):         # concurso cultural (soberanas) != concurso público
            n_offtype += 1
            continue
        used_title_fallback = False
        # Regla 1 — BINDING gana: el item nombra al certame padre (tipo + N/AAAA),
        # aunque el doc tenga su propio número. Colapsa docs de ciclo numerados.
        b = _BINDING.search(cita) or _BINDING.search(scope) or _BINDING.search(item_block)
        if b:
            btipo = _binding_bucket(b.group(1))
            if not _binding_bucket_compatible(btipo, bnorm, b.group(0), allow_cross_parent):
                n_offtype += 1
                continue
            key = _founds_certame(cita, item_block or scope, bnorm, other,
                                  text=text, idx=line_idx,
                                  allow_cross_parent=allow_cross_parent)
            if key:
                if key[0] == "Y" and _has_any_certame_for_year(certames, key[1]):
                    if is_accessory:
                        n_cycle += 1
                    continue
                certames.add(key)
                if is_accessory:
                    n_cycle += 1
            elif is_accessory:
                n_cycle += 1
            continue
        if not kw.search(type_scope):      # tipo del bucket por cita/bloque local
            # fallback: título declara el tipo sin ambigüedad y el item no tiene
            # marca del OTRO tipo ni cultural en su ventana local.
            if not (title_declares and not _KW[other].search(type_scope) and not _CULTURAL.search(type_scope)):
                n_offtype += 1
                continue
            used_title_fallback = True
        # Regla 2 — edital con número propio -> crea certame. Si era documento de
        # ciclo de un certame padre, la Regla 1 ya lo colapso por binding.
        key = _num_key(cita, scope)
        if key:
            founded = _founds_certame(cita, item_block or scope, bnorm, other,
                                      text=text, idx=line_idx,
                                      allow_cross_parent=allow_cross_parent)
            if founded:
                if founded[0] == "Y" and _has_any_certame_for_year(certames, founded[1]):
                    if is_accessory:
                        n_cycle += 1
                    continue
                certames.add(founded)
                if is_accessory:
                    n_cycle += 1
                continue
            if is_accessory or _DOC_WORD.search(type_scope):
                n_cycle += 1
                continue
        else:
            if used_title_fallback:
                n_offtype += 1
                continue
            # Regla 3 — keyword de ciclo sin número/binding -> doc huérfano.
            if is_accessory:
                n_cycle += 1
                continue
            # Regla 4 — keyword + año sin número -> crea certame por año.
            y = re.search(r"\b(20[12]\d)\b", scope)
            if (y
                    and not _has_following_number_meta(lines, line_idx, y.group(1))
                    and not (
                        line_idx is not None
                        and _nearby_numbered_certame(lines, line_idx, bnorm, other, y.group(1))
                    )
                    and not _has_title_certame_for_year(certames, y.group(1))):
                certames.add(("Y", y.group(1)))
    ev = {
        "n_certames": len(certames),
        "certames": sorted(certames)[:8],
        "verif": n_verif, "off_type": n_offtype,
        "ciclo": n_cycle, "ajenos": n_ajeno, "piso": n_piso,
        "binding_piso": n_binding_piso, "meta_floor": n_meta_floor,
        "strong_floor": n_strong_floor, "number_meta_floor": n_number_meta_floor,
        "title_floor": n_title_floor,
        "piso_blocked": block_piso, "item_here": item_here,
        "item_other": item_other,
        "title_declares": title_declares,
        "listing_shell": listing_shell,
    }
    ev["listing_declares"] = _listing_declares_bucket(title, bnorm, other, title_declares)
    if len(certames) >= 2:
        return "confirmar", ev
    if len(certames) == 1 and ev["listing_shell"]:
        ev["estado"] = "revisar_certame_unico"
        ev["motivo"] = "indice con estructura de listado pero un solo certame"
        ev["motivo_code"] = "revisar_sem:certame_unico"
        return "revisar", ev
    if len(certames) == 1:
        ev["estado"] = "revisar"
        ev["motivo"] = "un solo certame (posible detalle)"
        ev["motivo_code"] = "revisar_sem:certame_unico"
        return "revisar", ev
    if n_ajeno and not certames:
        ev["estado"] = "revisar"
        ev["motivo"] = "solo editais de emisor ajeno"
        ev["motivo_code"] = "revisar_sem:emisor_ajeno"
        return "revisar", ev
    ev["estado"] = "revisar"
    ev["motivo"] = "0 certames del tipo alvo"
    ev["motivo_code"] = "revisar_sem:0_certames"
    return "revisar", ev
