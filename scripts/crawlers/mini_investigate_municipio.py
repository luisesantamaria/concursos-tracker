"""One-municipio investigation mode for concursos/PSS URLs.

This is intentionally diagnostic: it runs the free crawler, Gemini grounded
search, deterministic verification, and a small Gemini audit for the chosen
URLs. It does not write the production CSV.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from grounded_deepsearch_municipios_a import (  # noqa: E402
    BAD_HOSTS,
    UF_NOME,
    UF_SIGLA,
    add_usage,
    ai_validate_route,
    api_key,
    best_verified,
    bucket_dominance_score,
    candidate_page_quality,
    clean_url,
    collect_tier1_candidate_records,
    collect_tier1_candidates,
    compact_space,
    discover_official_site_free,
    fetch,
    fetch_rendered,
    ground_discover,
    host_matches_municipio_rs,
    is_soft_404,
    norm,
    repair_text_encoding,
    resolve_redirect,
    should_try_rendered,
    source_label_score,
    title_case_municipio,
    verify_url,
    verified_specificity,
)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
        }
    )
    return s


def urls_from_text(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"https?://[^\s\]\)\"'<>]+", text or ""):
        url = clean_url(raw.rstrip(".,;:"))
        host = urlparse(url).netloc.lower()
        if not url or any(bad in host for bad in BAD_HOSTS) or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def enrich_from_candidate_pages(
    s: requests.Session,
    municipio: str,
    site_base: str,
    seeds: list[str],
    timeout: int,
    max_seeds: int,
    max_links: int,
    render: bool,
) -> list[str]:
    """Open candidate pages and add official links that mention selection terms."""
    out: list[str] = []
    seen: set[str] = set()
    terms = [
        "concurso",
        "concursos",
        "processo seletivo",
        "processos seletivos",
        "selecao publica",
        "selecoes publicas",
        "pss",
        "edital",
        "editais",
        "documentos",
        "publicacoes",
        "contratacao",
    ]
    for idx, seed in enumerate(seeds[:max_seeds], start=1):
        seed = clean_url(seed)
        if not seed or seed in seen or not host_matches_municipio_rs(seed, municipio, site_base):
            continue
        print(f"    enrich[{idx}/{min(len(seeds), max_seeds)}] {seed}", flush=True)
        seen.add(seed)
        out.append(seed)
        page = fetch(s, seed, timeout)
        if render and (should_try_rendered(page, "concursos") or should_try_rendered(page, "processos")):
            print("      static looked thin; rendering once", flush=True)
            rendered = fetch_rendered(s, seed, timeout)
            if rendered.status == 200 and rendered.text:
                page = rendered
        if page.status != 200 or is_soft_404(page):
            continue
        for link in getattr(page, "links", [])[:180]:
            href = clean_url(urljoin(page.url, link.get("href", "")))
            if not href or href in seen or not host_matches_municipio_rs(href, municipio, site_base):
                continue
            blob = norm(f"{href} {link.get('text', '')}")
            if any(t in blob for t in terms):
                seen.add(href)
                out.append(href)
                if len(out) >= max_links:
                    return out
    return out


def audit_selected(
    s: requests.Session,
    model: str,
    municipio: str,
    site_base: str,
    bucket: str,
    url: str,
    timeout: int,
    row_usage: dict,
) -> tuple[str, str]:
    """Always run the small AI verifier on a selected URL."""
    if not url:
        return "no_url", "No hay URL elegida para auditar."
    page = fetch(s, url, timeout)
    if should_try_rendered(page, bucket):
        rendered = fetch_rendered(s, url, timeout)
        if rendered.status == 200 and rendered.text:
            page = rendered
    if page.status != 200 or is_soft_404(page):
        return "reject", f"No carga bien o parece soft-404: status={page.status}."
    verdict, usage = ai_validate_route(s, model, page, municipio, bucket, timeout)
    add_usage(row_usage, usage)
    if verdict["route_valid"]:
        state = "accept_events" if verdict["content_has_events"] else "accept_empty_section"
    else:
        state = "reject"
    return state, verdict["reason"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("municipio")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--ai-timeout", type=int, default=60)
    parser.add_argument("--max-seeds", type=int, default=18)
    parser.add_argument("--max-enriched", type=int, default=70)
    parser.add_argument("--render-enrich", action="store_true")
    args = parser.parse_args()

    if not api_key():
        print("ERROR: missing GEMINI_API_KEY", file=sys.stderr)
        return 2

    municipio = title_case_municipio(repair_text_encoding(args.municipio))
    s = session()
    usage = {"_tokens_in": 0, "_tokens_out": 0, "_tokens_total": 0}
    t0 = time.time()

    print(f"=== MINI INVESTIGACION: {municipio} / {UF_SIGLA} ===", flush=True)

    home, probes = discover_official_site_free(s, municipio, args.timeout)
    site_base = clean_url(home.url) if home else ""
    print("\n[1] Site oficial gratis")
    print(f"site_base={site_base or 'NO_ENCONTRADO'}")
    for p in probes[:8]:
        print(f"  probe status={p.status} url={clean_url(p.url)} title={compact_space(getattr(p, 'title', '') or '')[:80]}")

    free_candidates: list[str] = []
    free_source_labels: dict[str, list[str]] = {}
    if home:
        for bucket in ["concursos", "processos"]:
            found, labels = collect_tier1_candidate_records(s, home, municipio, bucket, args.timeout)
            print(f"\n[2] Tier 1 candidatos {bucket}: {len(found)}")
            for u in found[:12]:
                label_txt = " | ".join(labels.get(u, [])[:2])
                print(f"  - {u}" + (f"  [menu: {label_txt[:140]}]" if label_txt else ""))
            free_candidates.extend(found)
            for u, vals in labels.items():
                free_source_labels.setdefault(u, [])
                for v in vals:
                    if v not in free_source_labels[u]:
                        free_source_labels[u].append(v)

    print("\n[3] Gemini mini-investigacion con Google Search")
    print("  llamando Gemini grounded...", flush=True)
    grounded_text, grounded_urls, ug = ground_discover(
        s,
        args.model,
        municipio,
        site_base,
        ["concursos publicos", "processos seletivos"],
        args.ai_timeout,
    )
    add_usage(usage, ug)
    text_urls = urls_from_text(grounded_text)
    print(f"grounded_urls={len(grounded_urls)} text_urls={len(text_urls)}")
    print("respuesta Gemini:")
    print(compact_space(grounded_text)[:1200])

    candidate_seeds = list(dict.fromkeys([site_base] + free_candidates + grounded_urls + text_urls))
    print("\n[4] Enriquecimiento local de paginas candidatas", flush=True)
    all_candidates = enrich_from_candidate_pages(
        s,
        municipio,
        site_base,
        candidate_seeds,
        args.timeout,
        args.max_seeds,
        args.max_enriched,
        args.render_enrich,
    )
    print(f"\n[4] Candidatos enriquecidos oficiales: {len(all_candidates)}")
    for u in all_candidates[:30]:
        print(f"  - {u}")

    results = {}
    for bucket in ["concursos", "processos"]:
        print(f"\n[5] Verificacion deterministica: {bucket}")
        checks = []
        for n, u in enumerate(all_candidates[:args.max_enriched], start=1):
            if not host_matches_municipio_rs(u, municipio, site_base):
                continue
            print(f"  verify[{n}/{min(len(all_candidates), args.max_enriched)}] {u}", flush=True)
            verified, note = verify_url(s, u, bucket, args.timeout)
            if verified or note not in {"missing_public_selection_signal", "missing_content_signal"}:
                scored_url = verified or u
                page = fetch(s, scored_url, args.timeout)
                if should_try_rendered(page, bucket):
                    rendered = fetch_rendered(s, scored_url, args.timeout)
                    if rendered.status and rendered.status < 400:
                        page = rendered
                specificity = verified_specificity(scored_url, note, bucket)
                page_ok = bool(page.status and page.status < 400 and page.text)
                dominance = bucket_dominance_score(page, bucket) if page_ok else 0
                quality, quality_note = candidate_page_quality(page, bucket, scored_url) if page_ok else (0, "fetch_failed")
                source_bonus, source_note = source_label_score(free_source_labels.get(u, []), bucket, scored_url)
                checks.append((u, verified, note, specificity + dominance + quality + source_bonus, f"{quality_note};{source_note}"))
        for u, verified, note, score, quality_note in sorted(checks, key=lambda x: x[3], reverse=True)[:20]:
            print(f"  score={score:>3} note={note:<35} q={quality_note} verified={verified or '-'} raw={u}")

        ranked_checks = sorted(checks, key=lambda x: x[3], reverse=True)
        if ranked_checks and ranked_checks[0][3] > 0:
            raw_url, verified_url, note, _score, _quality_note = ranked_checks[0]
            chosen = verified_url or raw_url
            chosen_note = f"mini_best_score:{note};{_quality_note}"
        else:
            chosen, chosen_note = best_verified(
                s,
                all_candidates,
                municipio,
                site_base,
                bucket,
                args.timeout,
                model=args.model,
                ai_timeout=args.ai_timeout,
                enable_route_ai=True,
                max_route_ai=20,
                route_ai_candidates=6,
                source_labels=free_source_labels,
                usage_sink=usage,
                notes_sink=[],
            )
        ai_state, ai_reason = audit_selected(
            s,
            args.model,
            municipio,
            site_base,
            bucket,
            chosen,
            args.ai_timeout,
            usage,
        )
        results[bucket] = {
            "url": chosen,
            "note": chosen_note,
            "ai_state": ai_state,
            "ai_reason": ai_reason,
        }

    print("\n=== RESULTADO ===")
    print(f"site_base: {site_base or 'NO_ENCONTRADO'}")
    for bucket, label in [("concursos", "url_concursos"), ("processos", "url_processos_seletivos")]:
        r = results[bucket]
        print(f"{label}: {r['url'] or 'NO_ENCONTRADA'}")
        print(f"  encontrado_por: {r['note'] or 'sin_match'}")
        print(f"  auditoria_ia: {r['ai_state']} - {r['ai_reason']}")
    print(
        f"tokens_total={usage['_tokens_total']} input={usage['_tokens_in']} output={usage['_tokens_out']} "
        f"elapsed={time.time() - t0:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
