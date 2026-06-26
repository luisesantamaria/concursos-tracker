#!/usr/bin/env python3
"""
Fase 2C - Registro de sites oficiales municipales (RS).

Construye un mapa reutilizable de municipios -> site oficial -> rutas de
concursos/processos seletivos/diario. Este archivo no intenta "publicar" un
concurso; solo prepara la base para el delta scanner:

  Ache radar -> concursos actuales
  Sites municipais -> fuentes oficiales a monitorear
  Delta scanner -> candidatos oficiales que Ache aun no trajo

Fuentes:
  - IBGE Localidades API: lista oficial de municipios RS.
  - CSV Fase 2 v2: URLs oficiales ya aprendidas por el pipeline.
  - Validacion automatica de hosts y rutas probables.
  - Diario Municipal FAMURS como adapter futuro por fecha/busqueda.

Salidas:
  data/sites_municipios_rs.csv
  data/sites_municipios.xlsx  (pestana RS)
  data/sites_municipios_rs.md
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

try:
    import requests
except Exception:  # pragma: no cover - fallback only when embedded env changes
    requests = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import write_table, write_xlsx, read_csv_dicts  # noqa: E402
import ache_rs_official_pipeline as ache  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PHASE2 = PROJECT_ROOT / "data" / "ache_rs_fase2_v2.csv"
DEFAULT_CSV = PROJECT_ROOT / "data" / "sites_municipios_rs.csv"
DEFAULT_XLSX = PROJECT_ROOT / "data" / "sites_municipios.xlsx"
DEFAULT_REPORT = PROJECT_ROOT / "data" / "sites_municipios_rs.md"
DEFAULT_FAMURS_GUIDE = PROJECT_ROOT / "data" / "catalog" / "famurs_guia_rs.xlsx"

IBGE_RS_MUNICIPIOS_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/43/municipios"
FAMURS_GUIA_XLSX_URL = "https://famurs.com.br/uploads/config/39900/GUIA_RS__SITE_FAMURS.xlsx"
DIARIO_FAMURS_SEARCH = "https://www.diariomunicipal.com.br/famurs/pesquisar"

CONCURSO_ROUTE_PROBES = (
    "editais/concursos-publicos",
    "Lista/3804/Concursos-Publicos",
    "concursos",
    "concurso",
    "site/concursos",
    "concursos-publicos",
    "concurso-publico",
    "processos-seletivos",
    "processo-seletivo",
    "pt_BR/concursos",
    "portal/editais/3",
    "publicacoes/concursos-publicos/",
    "transparencia/concursos",
    "portal-da-transparencia/concursos-publicos",
    "mural/concursos-1",
    "concurso/index?slug=lista-de-concursos-publicos",
    "autoatendimento/servicos/concursos-publicos",
    "autoatendimento/servicos/processos-seletivos",
)

CONCURSO_SIGNALS = (
    "concurso publico", "concursos publicos", "concurso público", "concursos públicos",
    "processo seletivo", "processos seletivos", "pss", "edital de abertura",
    "inscricoes", "inscrições",
)

NOISE_SIGNALS = (
    "licitacao", "licitação", "pregao", "pregão", "dispensa", "inexigibilidade",
    "leilao", "leilão", "aldir blanc", "concurso artistico", "concurso artístico",
    "soberanas", "rainha",
)

# Canonical route families. Kept separate because municipal sites often publish
# concurso publico and processo seletivo in sibling indexes, each with its own
# editais, retificacoes, convocacoes and resultados.
CONCURSO_ROUTE_PROBES = (
    "editais/concursos-publicos",
    "Lista/3804/Concursos-Publicos",
    "concursos",
    "concurso",
    "site/concursos",
    "concursos-publicos",
    "concurso-publico",
    "pt_BR/concursos",
    "portal/editais/3",
    "publicacoes/concursos-publicos/",
    "transparencia/concursos",
    "portal-da-transparencia/concursos-publicos",
    "mural/concursos-1",
    "concurso/index?slug=lista-de-concursos-publicos",
    "autoatendimento/servicos/concursos-publicos",
)

PROCESSO_ROUTE_PROBES = (
    "editais/processos-seletivos",
    "Lista/3822/Processos-seletivos",
    "processos-seletivos",
    "processo-seletivo",
    "site/processos-seletivos",
    "site/processo-seletivo",
    "processo-seletivo-simplificado",
    "processos-seletivos-simplificados",
    "seletivos",
    "pss",
    "autoatendimento/servicos/processos-seletivos",
)

CONCURSO_SIGNALS = (
    "concurso publico", "concursos publicos", "concurso público", "concursos públicos",
    "edital de abertura", "inscricoes", "inscrições",
)

PROCESSO_SIGNALS = (
    "processo seletivo", "processos seletivos", "processo seletivo simplificado",
    "processos seletivos simplificados", "pss", "edital de abertura",
    "inscricoes", "inscrições",
)

# Canonical noise list after the legacy mojibake-prone definitions above.
# These terms prevent generic "concurso" pages (photo contests, traffic
# campaigns, cultural awards) from being promoted as concurso publico indexes.
NOISE_SIGNALS = (
    "licitacao", "licitação", "licitaÃ§Ã£o", "pregao", "pregão", "pregÃ£o",
    "dispensa", "inexigibilidade", "leilao", "leilão", "leilÃ£o",
    "aldir blanc", "concurso artistico", "concurso artístico", "concurso artÃ­stico",
    "soberanas", "rainha", "fotografia", "fotografias", "trânsito", "transito",
    "concurso de fotografias", "educacao para o transito", "educação para o trânsito",
)

FIELDS = [
    "uf",
    "ibge_codigo",
    "municipio",
    "municipio_slug",
    "famurs_site_url",
    "famurs_associacao",
    "famurs_telefone",
    "home_url",
    "home_status",
    "home_confidence",
    "home_method",
    "concursos_url",
    "concursos_status",
    "concursos_confidence",
    "concursos_method",
    "processos_seletivos_url",
    "processos_seletivos_status",
    "processos_seletivos_confidence",
    "processos_seletivos_method",
    "diario_municipal_url",
    "diario_municipal_search_url",
    "diario_adapter_status",
    "phase2_known_urls",
    "route_candidates",
    "last_checked",
    "notes",
]


@dataclass
class Fetch:
    url: str
    status: int
    final_url: str
    body: str
    error: str = ""


def normalize(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def slug_compact(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def visible_text(raw_html: str) -> str:
    raw_html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw_html or "")
    raw_html = re.sub(r"(?s)<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", html.unescape(raw_html)).strip()


def fetch_url(url: str, timeout: int, session: Optional["requests.Session"] = None) -> Fetch:
    if requests is None:
        return Fetch(url, 0, "", "", "requests_missing")
    sess = session or requests.Session()
    try:
        res = sess.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 concursos-rs-source-registry/0.1",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        text = res.text if "text" in res.headers.get("content-type", "").lower() or res.text else ""
        return Fetch(url, res.status_code, res.url, text)
    except Exception as exc:  # noqa: BLE001
        return Fetch(url, 0, "", "", type(exc).__name__)


def load_ibge_municipios(timeout: int) -> List[Dict[str, str]]:
    if requests is None:
        raise RuntimeError("requests no disponible")
    res = requests.get(IBGE_RS_MUNICIPIOS_URL, timeout=timeout)
    res.raise_for_status()
    data = res.json()
    rows = []
    for item in sorted(data, key=lambda x: x["nome"]):
        rows.append({
            "uf": "RS",
            "ibge_codigo": str(item["id"]),
            "municipio": item["nome"],
            "municipio_slug": slug_compact(item["nome"]),
        })
    return rows


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    value = value.split()[0].strip(";,")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    return value


def download_famurs_guide(path: Path, timeout: int) -> None:
    if path.exists():
        return
    if requests is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    res = requests.get(FAMURS_GUIA_XLSX_URL, timeout=timeout)
    res.raise_for_status()
    path.write_bytes(res.content)


def load_famurs_guide(path: Path, timeout: int) -> Dict[str, Dict[str, str]]:
    download_famurs_guide(path, timeout)
    if not path.exists():
        return {}
    rows = read_csv_dicts(path) if path.suffix.lower() == ".csv" else []
    if not rows:
        from excel_utils import read_xlsx_dicts
        rows = read_xlsx_dicts(path)
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        name = row.get("MUNICÍPIO") or row.get("MUNICIPIO") or ""
        if not name:
            continue
        key = slug_compact(name)
        out[key] = {
            "famurs_site_url": normalize_url(row.get("SITE PREFEITURA", "")),
            "famurs_associacao": row.get("ASSOCIAÇÃO", "") or row.get("ASSOCIACAO", ""),
            "famurs_telefone": " ".join(p for p in [row.get("DDD", ""), row.get("TELEFONE PREFEITURA", "")] if p),
        }
    return out


def split_urls(value: str) -> List[str]:
    out: List[str] = []
    for part in str(value or "").split(" | "):
        part = part.strip()
        if part.startswith("http") and part not in out:
            out.append(part)
    return out


def detect_city_from_phase2(row: Dict[str, str]) -> str:
    blob = " ".join([row.get("orgao", ""), row.get("detalle_ache", ""), row.get("edital", "")])
    city = ache.detect_city(blob)
    return city.strip()


def known_urls_by_city(phase2_csv: Path) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if not phase2_csv.exists():
        return out
    for row in read_csv_dicts(phase2_csv):
        city = detect_city_from_phase2(row)
        if not city:
            continue
        key = slug_compact(city)
        urls = []
        for field in ("official_base_url", "v2_official_url", "official_source_urls"):
            urls.extend(split_urls(row.get(field, "")))
        for url in urls:
            host = urlparse(url).netloc.lower()
            if not (host.endswith(".rs.gov.br") or host.endswith("prefeitura.poa.br")):
                continue
            out.setdefault(key, [])
            if url not in out[key]:
                out[key].append(url)
    return out


def host_candidates(slug: str, famurs_site: str = "") -> List[str]:
    if not slug:
        return []
    candidates = []
    if famurs_site:
        candidates.append(famurs_site)
    candidates.extend([
        f"https://www.{slug}.rs.gov.br/",
        f"https://{slug}.rs.gov.br/",
    ])
    out: List[str] = []
    for url in candidates:
        if url and url not in out:
            out.append(url)
    return out


def is_home_valid(fetch: Fetch, municipio: str) -> Tuple[bool, str]:
    if fetch.status not in {200, 301, 302} or not fetch.body:
        return False, "not_loaded"
    text = normalize(visible_text(fetch.body)[:30000])
    if len(text) < 150:
        return False, "thin_page"
    public_signal = any(token in text for token in ("prefeitura", "municipio", "municipal", "gov br"))
    city_tokens = [t for t in normalize(municipio).split() if len(t) >= 4]
    city_signal = any(t in text for t in city_tokens[:3])
    if public_signal and city_signal:
        return True, "verified_prefeitura_city"
    if public_signal:
        return True, "verified_prefeitura_generic"
    return False, "no_prefeitura_signal"


def route_score(fetch: Fetch, signals: Sequence[str], url_terms: Sequence[str]) -> Tuple[int, str]:
    if fetch.status not in {200, 301, 302} or not fetch.body:
        return 0, "not_loaded"
    url_norm = normalize(fetch.final_url or fetch.url)
    text_norm = normalize(visible_text(fetch.body)[:20000])
    blob = f"{url_norm} {text_norm}"
    if any(noise in url_norm for noise in NOISE_SIGNALS):
        return 0, "noise"
    score = 0
    reasons = []
    if any(s in url_norm for s in url_terms):
        score += 4
        reasons.append("url_signal")
    for signal in signals:
        if signal in blob:
            score += 2
            reasons.append(signal)
    if re.search(r"20\d{2}", blob):
        score += 1
        reasons.append("year")
    return score, ",".join(reasons[:6])


def concurso_score(fetch: Fetch) -> Tuple[int, str]:
    return route_score(fetch, CONCURSO_SIGNALS, ("concursos", "concurso", "concursos publicos", "concurso publico"))


def processo_score(fetch: Fetch) -> Tuple[int, str]:
    return route_score(fetch, PROCESSO_SIGNALS, ("processos seletivos", "processo seletivo", "pss", "seletivos"))


def has_category_signal(reason: str) -> bool:
    parts = {part.strip() for part in (reason or "").split(",") if part.strip()}
    return bool(parts - {"url_signal", "year"})


def select_best_route(
    home_url: str,
    timeout: int,
    route_probes: Sequence[str],
    scorer,
) -> Tuple[str, str, str, List[str]]:
    parsed_home = urlparse(home_url)
    root = f"{parsed_home.scheme}://{parsed_home.netloc}/" if parsed_home.scheme and parsed_home.netloc else home_url
    candidates = [urljoin(root, route) for route in route_probes]
    best_url = ""
    best_status = ""
    best_conf = ""
    seen_candidates: List[str] = []
    with requests.Session() as session:
        for url in candidates:
            fetch = fetch_url(url, timeout, session)
            score, reason = scorer(fetch)
            if fetch.status in {200, 301, 302} and reason != "noise":
                final = fetch.final_url or url
                if final not in seen_candidates:
                    seen_candidates.append(final)
            if score >= 5 and has_category_signal(reason) and not best_url:
                best_url = fetch.final_url or url
                best_status = str(fetch.status)
                best_conf = f"{score}:{reason}"
                # A validated generic concursos route is enough; no need to keep
                # hammering small municipal sites.
                break
    return best_url, best_status, best_conf, seen_candidates[:20]


def infer_home_from_known(urls: Sequence[str]) -> str:
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


def build_one(
    row: Dict[str, str],
    known: Dict[str, List[str]],
    famurs: Dict[str, Dict[str, str]],
    args: argparse.Namespace,
) -> Dict[str, str]:
    slug = row["municipio_slug"]
    famurs_info = famurs.get(slug, {})
    famurs_site = famurs_info.get("famurs_site_url", "")
    known_urls = known.get(slug, [])
    home_url = infer_home_from_known(known_urls)
    home_status = ""
    home_confidence = ""
    home_method = ""
    notes: List[str] = []
    route_candidates: List[str] = []

    with requests.Session() as session:
        if home_url:
            fetch = fetch_url(home_url, args.timeout, session)
            ok, reason = is_home_valid(fetch, row["municipio"])
            if ok:
                home_url = fetch.final_url or home_url
                home_status = str(fetch.status)
                home_confidence = reason
                home_method = "phase2_known_host"
            else:
                notes.append(f"known_home_unverified:{reason}")
                home_url = ""

        if not home_url:
            for candidate in host_candidates(slug, famurs_site):
                fetch = fetch_url(candidate, args.timeout, session)
                ok, reason = is_home_valid(fetch, row["municipio"])
                if ok:
                    home_url = fetch.final_url or candidate
                    home_status = str(fetch.status)
                    home_confidence = reason
                    home_method = "famurs_site" if candidate == famurs_site else "common_rs_gov_br_host"
                    break
                if fetch.error:
                    notes.append(f"{candidate}:{fetch.error}")

    concursos_url = ""
    concursos_status = ""
    concursos_confidence = ""
    concursos_method = ""
    processos_url = ""
    processos_status = ""
    processos_confidence = ""
    processos_method = ""

    # First trust specific routes already learned by the official resolver if
    # they look like a concursos or processos section.
    for url in known_urls:
        fetch = fetch_url(url, args.timeout)
        score, reason = concurso_score(fetch)
        if score >= 5 and has_category_signal(reason) and not concursos_url:
            concursos_url = url
            concursos_status = "known"
            concursos_confidence = f"{score}:{reason}"
            concursos_method = "phase2_known_route"
        p_score, p_reason = processo_score(fetch)
        if p_score >= 5 and has_category_signal(p_reason) and not processos_url:
            processos_url = url
            processos_status = "known"
            processos_confidence = f"{p_score}:{p_reason}"
            processos_method = "phase2_known_route"

    if not concursos_url and home_url and args.probe_routes:
        concursos_url, concursos_status, concursos_confidence, route_candidates = select_best_route(
            home_url,
            args.timeout,
            CONCURSO_ROUTE_PROBES,
            concurso_score,
        )
        if concursos_url:
            concursos_method = "route_probe"
    processo_route_candidates: List[str] = []
    if not processos_url and home_url and args.probe_routes:
        processos_url, processos_status, processos_confidence, processo_route_candidates = select_best_route(
            home_url,
            args.timeout,
            PROCESSO_ROUTE_PROBES,
            processo_score,
        )
        if processos_url:
            processos_method = "route_probe"
    if processo_route_candidates:
        route_candidates.extend([u for u in processo_route_candidates if u not in route_candidates])

    diario_search = f"{DIARIO_FAMURS_SEARCH}?q={quote_plus(row['municipio'])}"
    return {
        **row,
        "famurs_site_url": famurs_site,
        "famurs_associacao": famurs_info.get("famurs_associacao", ""),
        "famurs_telefone": famurs_info.get("famurs_telefone", ""),
        "home_url": home_url,
        "home_status": home_status,
        "home_confidence": home_confidence,
        "home_method": home_method,
        "concursos_url": concursos_url,
        "concursos_status": concursos_status,
        "concursos_confidence": concursos_confidence,
        "concursos_method": concursos_method,
        "processos_seletivos_url": processos_url,
        "processos_seletivos_status": processos_status,
        "processos_seletivos_confidence": processos_confidence,
        "processos_seletivos_method": processos_method,
        "diario_municipal_url": DIARIO_FAMURS_SEARCH,
        "diario_municipal_search_url": diario_search,
        "diario_adapter_status": "pending_date_search_adapter",
        "phase2_known_urls": " | ".join(known_urls),
        "route_candidates": json.dumps(route_candidates, ensure_ascii=False),
        "last_checked": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "notes": " | ".join(notes),
    }


def write_report(rows: List[Dict[str, str]], path: Path) -> None:
    total = len(rows)
    home = sum(1 for r in rows if r["home_url"])
    concursos = sum(1 for r in rows if r["concursos_url"])
    processos = sum(1 for r in rows if r["processos_seletivos_url"])
    phase2 = sum(1 for r in rows if r["phase2_known_urls"])
    lines = [
        "# Fase 2C - Sites municipais RS",
        "",
        f"- Municipios IBGE RS: {total}",
        f"- Home municipal verificada: {home}/{total}",
        f"- Ruta concursos publicos validada: {concursos}/{total}",
        f"- Ruta processos seletivos validada: {processos}/{total}",
        f"- Municipios con URLs aprendidas de Fase 2: {phase2}/{total}",
        f"- Diario Municipal FAMURS: {DIARIO_FAMURS_SEARCH}",
        "",
        "## Fuentes",
        "",
        f"- IBGE Localidades API: {IBGE_RS_MUNICIPIOS_URL}",
        f"- FAMURS Guia RS XLSX: {FAMURS_GUIA_XLSX_URL}",
        f"- FAMURS/Diario Municipal: {DIARIO_FAMURS_SEARCH}",
        "",
        "## Notas",
        "",
        "- `home_url` es el dominio municipal validado.",
        "- `concursos_url` solo se llena si la ruta carga y contiene senales de concurso publico.",
        "- `processos_seletivos_url` solo se llena si la ruta carga y contiene senales de processo seletivo.",
        "- Estas paginas suelen ser indices/listas; los editais, convocacoes y resultados se extraeran despues en el delta scanner.",
        "- `diario_adapter_status=pending_date_search_adapter` marca trabajo futuro: buscar por fecha/termino en Diario Municipal.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fase 2C - sites oficiales municipales RS")
    parser.add_argument("--phase2", default=str(DEFAULT_PHASE2))
    parser.add_argument("--out", default=str(DEFAULT_CSV))
    parser.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--famurs-guide", default=str(DEFAULT_FAMURS_GUIDE))
    parser.add_argument("--timeout", type=int, default=6)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-probe-routes", dest="probe_routes", action="store_false")
    parser.set_defaults(probe_routes=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    municipios = load_ibge_municipios(args.timeout)
    if args.limit:
        municipios = municipios[: args.limit]
    known = known_urls_by_city(Path(args.phase2))
    famurs = load_famurs_guide(Path(args.famurs_guide), args.timeout)
    print(f"Fase 2C - sites municipais RS")
    print(f"  municipios: {len(municipios)}")
    print(f"  phase2 urls: {sum(len(v) for v in known.values())} urls / {len(known)} municipios")
    print(f"  famurs guia: {len(famurs)} municipios")
    print(f"  route probes: {'SI' if args.probe_routes else 'NO'}")

    rows: List[Dict[str, str]] = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {pool.submit(build_one, row, known, famurs, args): row for row in municipios}
        for idx, future in enumerate(as_completed(future_map), start=1):
            base = future_map[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001
                rows.append({
                    **base,
                    "famurs_site_url": famurs.get(base["municipio_slug"], {}).get("famurs_site_url", ""),
                    "famurs_associacao": famurs.get(base["municipio_slug"], {}).get("famurs_associacao", ""),
                    "famurs_telefone": famurs.get(base["municipio_slug"], {}).get("famurs_telefone", ""),
                    "home_url": "",
                    "home_status": "",
                    "home_confidence": "",
                    "home_method": "",
                    "concursos_url": "",
                    "concursos_status": "",
                    "concursos_confidence": "",
                    "concursos_method": "",
                    "processos_seletivos_url": "",
                    "processos_seletivos_status": "",
                    "processos_seletivos_confidence": "",
                    "processos_seletivos_method": "",
                    "diario_municipal_url": DIARIO_FAMURS_SEARCH,
                    "diario_municipal_search_url": f"{DIARIO_FAMURS_SEARCH}?q={quote_plus(base['municipio'])}",
                    "diario_adapter_status": "pending_date_search_adapter",
                    "phase2_known_urls": "",
                    "route_candidates": "[]",
                    "last_checked": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "notes": f"error:{type(exc).__name__}",
                })
            if idx % 25 == 0:
                print(f"  {idx:03d}/{len(municipios)} procesados")

    rows.sort(key=lambda r: normalize(r["municipio"]))
    out = Path(args.out)
    write_table(rows, FIELDS, out, sheet_name="RS")
    xlsx = write_xlsx(rows, FIELDS, Path(args.xlsx), sheet_name="RS")
    write_report(rows, Path(args.report))

    print("\n=============== FASE 2C - SITES MUNICIPAIS RS ===============")
    print(f"  Municipios                       : {len(rows)}")
    print(f"  Home municipal verificada         : {sum(1 for r in rows if r['home_url'])}")
    print(f"  Ruta concursos publicos validada  : {sum(1 for r in rows if r['concursos_url'])}")
    print(f"  Ruta processos seletivos validada : {sum(1 for r in rows if r['processos_seletivos_url'])}")
    print(f"  CSV   : {out.resolve()}")
    print(f"  Excel : {xlsx.resolve()}")
    print(f"  Report: {Path(args.report).resolve()}")
    print(f"  Tiempo: {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
