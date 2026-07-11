"""Deterministic hierarchical municipality sampler for offline staging."""

from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.fase2_municipios import cascade_municipios as cascade


SCHEMA_VERSION = 1
STATE_VOCABULARY = frozenset({
    "confirmado", "revisar", "nao_encontrado", "misto", "sem_saida_previa",
})
BORDERLINE_REASON_ORDER = ("v1_revisar", "familia_dificil", "senal_ambigua")
DEFAULT_FAMILY_TABLE = Path(__file__).with_name("portal_families_v1.json")
URL_FIELDS = (
    "site_base", "url_concursos", "url_processos_seletivos", "url_editais",
    "url_convocacoes", "url_diario_publicacoes",
)
RESOURCE_URL_FIELDS = URL_FIELDS[1:]
GOLDEN_URL_FIELDS = (
    "site_base", "url_concursos", "url_processos_seletivos",
    "urls_concursos_extra", "urls_processos_extra",
)
MAX_ROWS = 10_000
MAX_TEXT = 8_000


class SelectorDataError(ValueError):
    """Input data cannot satisfy the closed selector contract."""


def _text(value: Any, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SelectorDataError(f"{field} must be text")
    value = value.strip()
    if len(value) > MAX_TEXT:
        raise SelectorDataError(f"{field} exceeds size limit")
    return value


def load_family_table(path: Path = DEFAULT_FAMILY_TABLE) -> Mapping[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectorDataError("invalid family table") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SelectorDataError("unsupported family table schema")
    families = raw.get("families")
    if not isinstance(families, list) or not families:
        raise SelectorDataError("family table requires ordered families")
    names: list[str] = []
    for rule in families:
        if not isinstance(rule, dict):
            raise SelectorDataError("family rule must be an object")
        name = rule.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise SelectorDataError("family names must be unique text")
        if not isinstance(rule.get("difficult"), bool):
            raise SelectorDataError("family difficult flag must be boolean")
        for key in ("host_suffixes", "url_contains"):
            values = rule.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise SelectorDataError(f"invalid {key} in family table")
        names.append(name)
    fallback = raw.get("fallback")
    if not isinstance(fallback, str) or not fallback or fallback in names:
        raise SelectorDataError("invalid family fallback")
    return raw


def map_state(row: Mapping[str, str]) -> tuple[str, Mapping[str, str]]:
    concursos = _text(row.get("status_concursos", ""), field="status_concursos")
    processos = _text(
        row.get("status_processos_seletivos", ""),
        field="status_processos_seletivos",
    )
    source = {"concursos": concursos, "processos_seletivos": processos}
    if not concursos and not processos:
        return "sem_saida_previa", source
    source_mapping = {
        "boa": "confirmado",
        "nao_encontrada": "nao_encontrado",
        "revisar": "revisar",
    }
    values: list[str] = []
    for value in (concursos, processos):
        if not value:
            continue
        mapped = source_mapping.get(value)
        if mapped is None:
            raise SelectorDataError(f"unmapped V1 state: {value}")
        values.append(mapped)
    if not values:
        return "sem_saida_previa", source
    if "revisar" in values:
        return "revisar", source
    if len(set(values)) == 1:
        return values[0], source
    return "misto", source


def _urls(row: Mapping[str, str], fields: Sequence[str]) -> list[str]:
    return [
        value for field in fields
        if (value := _text(row.get(field, ""), field=field))
    ]


def _host(url: str) -> str:
    candidate = url if "://" in url else f"http://{url}"
    try:
        return (urlsplit(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_matches(host: str, suffix: str) -> bool:
    suffix = suffix.lower().lstrip(".")
    return bool(host and (host == suffix or host.endswith(f".{suffix}")))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _canonical_resource(url: str) -> str:
    if not url or url.casefold() == "no_existe":
        return ""
    return cascade._normalized_candidate_url(url)


def classify_candidate(row: Mapping[str, str], table: Mapping[str, Any]) -> dict[str, Any]:
    municipio = _text(row.get("municipio", ""), field="municipio")
    if not municipio:
        raise SelectorDataError("municipio is required")
    uf = _text(row.get("uf", ""), field="uf")
    if uf and uf != "RS":
        raise SelectorDataError("selector universe must be RS")
    urls = _urls(row, URL_FIELDS)
    lowered_urls = [url.casefold() for url in urls]
    hosts = [_host(url) for url in urls]
    hosts = [host for host in hosts if host]
    family = str(table["fallback"])
    difficult = False
    for rule in table["families"]:
        host_match = any(
            _host_matches(host, suffix)
            for host in hosts for suffix in rule.get("host_suffixes", [])
        )
        content_match = any(
            fragment.casefold() in url
            for url in lowered_urls for fragment in rule.get("url_contains", [])
        )
        if host_match or content_match:
            family = rule["name"]
            difficult = rule["difficult"]
            break

    site_host = _host(_text(row.get("site_base", ""), field="site_base"))
    resource_urls = _urls(row, RESOURCE_URL_FIELDS)
    resource_hosts = [_host(url) for url in resource_urls]
    resource_hosts = [host for host in resource_hosts if host]
    signals = {
        "ip_delegado": any(_is_ip(host) for host in hosts),
        "multiples_hosts": len(set(hosts)) > 1,
        "usa_transparencia_externa": any(
            "transparencia" in url.casefold()
            and bool(host := _host(url))
            and bool(site_host)
            and host != site_host
            for url in resource_urls
        ),
    }
    state, state_source = map_state(row)
    reasons = []
    if state == "revisar":
        reasons.append("v1_revisar")
    if difficult:
        reasons.append("familia_dificil")
    if any(signals.values()):
        reasons.append("senal_ambigua")
    reasons = [reason for reason in BORDERLINE_REASON_ORDER if reason in reasons]
    return {
        "identity": f"municipio:{cascade.norm(municipio)}",
        "uf": uf or "RS",
        "municipio": municipio,
        "ibge": _text(row.get("ibge", ""), field="ibge"),
        "site_base": _text(row.get("site_base", ""), field="site_base"),
        "familia_portal": family,
        "signals": signals,
        "estado": state,
        "estado_fuente": state_source,
        "borderline": bool(reasons),
        "borderline_reasons": reasons,
    }


def _golden_identity_sets(
    golden_rows: Sequence[Mapping[str, str]],
) -> tuple[set[str], set[str]]:
    municipalities: set[str] = set()
    resources: set[str] = set()
    for row in golden_rows:
        name = _text(row.get("municipio", ""), field="golden.municipio")
        if name:
            municipalities.add(f"municipio:{cascade.norm(name)}")
        for url in _urls(row, GOLDEN_URL_FIELDS):
            normalized = _canonical_resource(url)
            if normalized:
                resources.add(f"resource:{normalized}")
    return municipalities, resources


def _candidate_resource_identities(row: Mapping[str, str]) -> set[str]:
    return {
        f"resource:{normalized}"
        for url in _urls(row, URL_FIELDS)
        if (normalized := _canonical_resource(url))
    }


def _validate_integer(value: int, *, field: str, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SelectorDataError(f"{field} must be integer >= {minimum}")


def _next_for_family(
    family: str,
    *,
    strata: Mapping[tuple[str, str], list[dict[str, Any]]],
    states_by_family: Mapping[str, tuple[str, ...]],
    cursors: dict[str, int],
    selected: set[str],
    borderline_only: bool,
) -> dict[str, Any] | None:
    states = states_by_family[family]
    if not states:
        return None
    start = cursors.get(family, 0) % len(states)
    for offset in range(len(states)):
        position = (start + offset) % len(states)
        state = states[position]
        for candidate in strata[(family, state)]:
            if candidate["identity"] in selected:
                continue
            if borderline_only and not candidate["borderline"]:
                continue
            cursors[family] = (position + 1) % len(states)
            return candidate
    return None


def _round_robin(
    *,
    families: Sequence[str],
    limit: int,
    selection_phase: str,
    strata: Mapping[tuple[str, str], list[dict[str, Any]]],
    states_by_family: Mapping[str, tuple[str, ...]],
    selected: set[str],
    output: list[dict[str, Any]],
    borderline_only: bool,
) -> None:
    cursors: dict[str, int] = {}
    while len(output) < limit:
        progressed = False
        for family in families:
            if len(output) >= limit:
                break
            candidate = _next_for_family(
                family,
                strata=strata,
                states_by_family=states_by_family,
                cursors=cursors,
                selected=selected,
                borderline_only=borderline_only,
            )
            if candidate is None:
                continue
            materialized = dict(candidate)
            materialized["selection_phase"] = selection_phase
            output.append(materialized)
            selected.add(candidate["identity"])
            progressed = True
        if not progressed:
            break


def select_sample(
    rows: Sequence[Mapping[str, str]],
    golden_rows: Sequence[Mapping[str, str]],
    *,
    size: int = 50,
    seed: int = 0,
    borderline_minimum: int = 10,
    table: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_integer(size, field="size", minimum=1)
    _validate_integer(seed, field="seed", minimum=0)
    _validate_integer(borderline_minimum, field="borderline_minimum", minimum=0)
    if len(rows) > MAX_ROWS or len(golden_rows) > MAX_ROWS:
        raise SelectorDataError("input row limit exceeded")
    table = table or load_family_table()
    golden_municipalities, golden_resources = _golden_identity_sets(golden_rows)

    eligible: list[dict[str, Any]] = []
    excluded = 0
    seen: set[str] = set()
    for row in rows:
        candidate = classify_candidate(row, table)
        identity = candidate["identity"]
        if identity in seen:
            raise SelectorDataError(f"duplicate municipality identity: {identity}")
        seen.add(identity)
        if (
            identity in golden_municipalities
            or bool(_candidate_resource_identities(row) & golden_resources)
        ):
            excluded += 1
            continue
        eligible.append(candidate)
    if len(eligible) < size:
        raise SelectorDataError(
            f"requested {size} municipalities but only {len(eligible)} remain"
        )

    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in sorted(eligible, key=lambda item: item["identity"]):
        strata[(candidate["familia_portal"], candidate["estado"])].append(candidate)
    rng = random.Random(seed)
    for key in sorted(strata):
        strata[key].sort(key=lambda item: item["identity"])
        rng.shuffle(strata[key])
    families = sorted({family for family, _state in strata})
    states_by_family = {
        family: tuple(sorted(state for fam, state in strata if fam == family))
        for family in families
    }

    borderline_available = sum(candidate["borderline"] for candidate in eligible)
    borderline_target = min(borderline_minimum, borderline_available, size)
    selected_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    _round_robin(
        families=families,
        limit=borderline_target,
        selection_phase="borderline_reserve",
        strata=strata,
        states_by_family=states_by_family,
        selected=selected_ids,
        output=selected,
        borderline_only=True,
    )
    _round_robin(
        families=families,
        limit=size,
        selection_phase="family_fill",
        strata=strata,
        states_by_family=states_by_family,
        selected=selected_ids,
        output=selected,
        borderline_only=False,
    )
    if len(selected) != size:
        raise SelectorDataError("hierarchical redistribution could not fill sample")

    family_counts = Counter(item["familia_portal"] for item in selected)
    state_counts = Counter(item["estado"] for item in selected)
    phase_counts = Counter(item["selection_phase"] for item in selected)
    signal_counts = {
        signal: sum(item["signals"][signal] for item in selected)
        for signal in sorted(next(iter(selected))["signals"])
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "seed": seed,
        "requested_size": size,
        "borderline_minimum": borderline_minimum,
        "universe_size": len(rows),
        "eligible_size": len(eligible),
        "excluded_golden_count": excluded,
        "strata_order": [f"{family}|{state}" for family, state in sorted(strata)],
        "coverage": {
            "families": dict(sorted(family_counts.items())),
            "states": dict(sorted(state_counts.items())),
            "borderline": sum(item["borderline"] for item in selected),
            "borderline_available": borderline_available,
            "phases": dict(sorted(phase_counts.items())),
            "signals": signal_counts,
        },
        "selected": selected,
    }


def canonical_json_bytes(artifact: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def derived_csv_bytes(artifact: Mapping[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    fields = (
        "identity", "uf", "municipio", "ibge", "site_base",
        "familia_portal", "estado", "estado_concursos_fuente",
        "estado_processos_fuente", "borderline", "borderline_reasons",
        "ip_delegado", "multiples_hosts", "usa_transparencia_externa",
        "selection_phase",
    )
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for item in artifact["selected"]:
        writer.writerow({
            "identity": item["identity"],
            "uf": item["uf"],
            "municipio": item["municipio"],
            "ibge": item["ibge"],
            "site_base": item["site_base"],
            "familia_portal": item["familia_portal"],
            "estado": item["estado"],
            "estado_concursos_fuente": item["estado_fuente"]["concursos"],
            "estado_processos_fuente": item["estado_fuente"]["processos_seletivos"],
            "borderline": json.dumps(item["borderline"]),
            "borderline_reasons": json.dumps(
                item["borderline_reasons"], ensure_ascii=False, separators=(",", ":")
            ),
            "ip_delegado": json.dumps(item["signals"]["ip_delegado"]),
            "multiples_hosts": json.dumps(item["signals"]["multiples_hosts"]),
            "usa_transparencia_externa": json.dumps(
                item["signals"]["usa_transparencia_externa"]
            ),
            "selection_phase": item["selection_phase"],
        })
    return output.getvalue().encode("utf-8")


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SelectorDataError(f"invalid CSV: {path.name}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic hierarchical RS municipality selector",
    )
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--family-table", type=Path, default=DEFAULT_FAMILY_TABLE)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--size", type=int, default=50)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--borderline-minimum", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact = select_sample(
        _read_csv(args.universe),
        _read_csv(args.golden),
        size=args.size,
        seed=args.seed,
        borderline_minimum=args.borderline_minimum,
        table=load_family_table(args.family_table),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_bytes(canonical_json_bytes(artifact))
    args.output_csv.write_bytes(derived_csv_bytes(artifact))
    print(json.dumps(artifact["coverage"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
