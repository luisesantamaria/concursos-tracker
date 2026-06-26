"""Hard RS scope guard for the authority-first pipeline.

Every crawler that reads a national banca must pass candidates through this
module before writing them to authority_first/data/raw.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = PROJECT_ROOT / "data" / "sites_municipios_rs.csv"
OTHER_UFS = {
    "AC",
    "AL",
    "AP",
    "AM",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MT",
    "MS",
    "MG",
    "PA",
    "PB",
    "PR",
    "PE",
    "PI",
    "RJ",
    "RN",
    "RO",
    "RR",
    "SC",
    "SP",
    "SE",
    "TO",
}
OTHER_STATE_NAMES = {
    "acre",
    "alagoas",
    "amapa",
    "amazonas",
    "bahia",
    "ceara",
    "distrito federal",
    "espirito santo",
    "goias",
    "maranhao",
    "mato grosso",
    "mato grosso do sul",
    "minas gerais",
    "para",
    "paraiba",
    "parana",
    "pernambuco",
    "piaui",
    "rio de janeiro",
    "rio grande do norte",
    "rondonia",
    "roraima",
    "santa catarina",
    "sao paulo",
    "sergipe",
    "tocantins",
}


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_slug(value: str) -> str:
    return normalize_text(value).replace(" ", "-")


@dataclass(frozen=True)
class RSScopeRegistry:
    municipalities: frozenset[str]
    slugs: frozenset[str]
    official_hosts: frozenset[str]

    @classmethod
    def from_csv(cls, path: Path = DEFAULT_REGISTRY) -> "RSScopeRegistry":
        municipalities: set[str] = set()
        slugs: set[str] = set()
        hosts: set[str] = set()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            first = handle.readline()
            handle.seek(0)
            if first.startswith("sep="):
                handle.readline()
            reader = csv.DictReader(handle, delimiter=";")
            for row in reader:
                if (row.get("uf") or "").upper() != "RS":
                    continue
                municipio = row.get("municipio") or ""
                slug = row.get("municipio_slug") or normalize_slug(municipio)
                if municipio:
                    municipalities.add(normalize_text(municipio))
                if slug:
                    slugs.add(normalize_slug(slug))
                for field in ("home_url", "concursos_url", "processos_seletivos_url", "diario_municipal_url"):
                    host = urlparse(row.get(field) or "").netloc.lower()
                    if host:
                        hosts.add(host.removeprefix("www."))
        return cls(frozenset(municipalities), frozenset(slugs), frozenset(hosts))


def candidate_rs_evidence(
    *,
    title: str = "",
    context: str = "",
    url: str = "",
    uf: str = "",
    municipio: str = "",
    registry: RSScopeRegistry | None = None,
) -> list[str]:
    registry = registry or RSScopeRegistry.from_csv()
    evidence: list[str] = []

    explicit_uf = (uf or "").upper().strip()
    if explicit_uf and explicit_uf != "RS":
        return []
    if explicit_uf == "RS":
        evidence.append("uf_equals_rs")

    raw_combined = " ".join([title or "", context or "", url or ""])
    other_uf_pattern = r"(?:/|\b-|[-\s])\s*(" + "|".join(sorted(OTHER_UFS)) + r")\b"
    has_other_uf = bool(re.search(other_uf_pattern, raw_combined, flags=re.I))
    has_rs = bool(re.search(r"(?:/|\b-|[-\s])\s*RS\b|rio grande do sul|\.rs\.gov\.br", raw_combined, flags=re.I))
    combined_for_state_names = normalize_text(raw_combined)
    has_other_state_name = any(re.search(rf"\b{re.escape(state)}\b", combined_for_state_names) for state in OTHER_STATE_NAMES)
    if (has_other_uf or has_other_state_name) and not has_rs:
        return []

    municipio_norm = normalize_text(municipio)
    municipio_slug = normalize_slug(municipio)
    if municipio_norm and municipio_norm in registry.municipalities:
        evidence.append("municipality_name_in_rs_registry")
    if municipio_slug and municipio_slug in registry.slugs:
        evidence.append("municipality_slug_in_rs_registry")

    combined = normalize_text(" ".join([title, context, url]))
    if re.search(r"\brs\b|rio grande do sul", combined):
        evidence.append("explicit_rs_context")
    for city in registry.municipalities:
        if city and re.search(rf"\b{re.escape(city)}\b", combined):
            evidence.append("bank_context_contains_rs_municipality")
            break

    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    if host and host in registry.official_hosts:
        evidence.append("official_prefeitura_url_matches_rs_registry")

    return evidence


def is_rs_candidate(**kwargs: str) -> bool:
    return bool(candidate_rs_evidence(**kwargs))


def require_rs_candidate(**kwargs: str) -> None:
    evidence = candidate_rs_evidence(**kwargs)
    if not evidence:
        raise ValueError("Candidate rejected: no hard RS evidence")
