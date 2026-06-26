from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


BASE_FIELDS = [
    "ano",
    "semaforo",
    "tipo",
    "orgao",
    "municipio",
    "uf",
    "numero",
    "banca",
    "edital_pagina",
    "edital_pdf",
    "ai_comment",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if lines and lines[0].startswith("sep="):
        lines = lines[1:]
    delimiter = ";" if lines and lines[0].count(";") >= lines[0].count(",") else ","
    reader = csv.DictReader(lines, delimiter=delimiter)
    return [dict(row) for row in reader]


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    keep = {}
    if "concurso" in query:
        keep["concurso"] = query["concurso"]
    path = parsed.path
    if parsed.netloc.lower().endswith("fundatec.org.br") and keep.get("concurso"):
        path = "/portal/concursos/pagina_editais.php"
    rebuilt = parsed._replace(
        scheme=parsed.scheme.lower() or "https",
        netloc=parsed.netloc.lower(),
        path=path,
        query=urlencode(keep, doseq=True),
        fragment="",
    )
    return urlunparse(rebuilt).rstrip("/")


def number_year(numero: str) -> str:
    match = re.search(r"/\s*(20\d{2})\b", numero or "")
    return match.group(1) if match else ""


def merge_comment(existing: str, addition: str) -> str:
    parts = [p for p in [existing, addition] if p]
    return " | ".join(dict.fromkeys(parts))


def dedupe_key(row: dict[str, str]) -> tuple[str, str, str, str, str, str]:
    page = normalize_url(row.get("edital_pagina", ""))
    pdf = normalize_url(row.get("edital_pdf", ""))
    return (
        norm_text(row.get("banca", "")),
        page,
        pdf,
        norm_text(row.get("tipo", "")),
        norm_text(row.get("orgao", "")),
        norm_text(row.get("numero", "")),
    )


def canonicalize_row(row: dict[str, str]) -> dict[str, str]:
    out = dict(row)
    page = out.get("edital_pagina", "")
    parsed = urlparse(page)
    qs = parse_qs(parsed.query)
    concurso_id = (qs.get("concurso") or [""])[0]
    if concurso_id and parsed.netloc.lower().endswith("fundatec.org.br"):
        out["edital_pagina"] = f"https://www.fundatec.org.br/portal/concursos/pagina_editais.php?concurso={concurso_id}"
    return out


def choose_keeper(rows: list[dict[str, str]]) -> dict[str, str]:
    wanted_year = number_year(rows[0].get("numero", ""))
    if wanted_year:
        for row in rows:
            if row.get("ano") == wanted_year:
                return row
    return sorted(rows, key=lambda row: row.get("ano", ""))[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Collapse false duplicate base events while preserving distinct editais.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    rows = read_csv(args.input)
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, str]]] = {}
    for original in rows:
        row = canonicalize_row(original)
        groups.setdefault(dedupe_key(row), []).append(row)

    out: list[dict[str, str]] = []
    collapsed = 0
    for group_rows in groups.values():
        if len(group_rows) == 1:
            out.append(group_rows[0])
            continue
        keeper = dict(choose_keeper(group_rows))
        years = sorted({row.get("ano", "") for row in group_rows if row.get("ano")})
        keeper["ai_comment"] = merge_comment(
            keeper.get("ai_comment", ""),
            f"Deduplicado: evento base repetido em anos {', '.join(years)}; preservada a linha do ano do edital quando possível.",
        )
        out.append(keeper)
        collapsed += len(group_rows) - 1

    fields = list(rows[0].keys()) if rows else BASE_FIELDS
    out.sort(key=lambda row: (row.get("ano", ""), row.get("banca", ""), row.get("municipio", ""), row.get("orgao", ""), row.get("numero", "")))
    write_csv(args.output, out, fields)
    print(f"INPUT_ROWS {len(rows)}")
    print(f"OUTPUT_ROWS {len(out)}")
    print(f"COLLAPSED_FALSE_DUPLICATES {collapsed}")
    print(f"OUT {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
