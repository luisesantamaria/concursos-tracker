#!/usr/bin/env python3
"""
Tester local v2 para bancas/fuentes bloqueadas.

Escalera de motores:
  1) requests: rapido, suficiente para sitios que solo bloqueaban Colab.
  2) curl_cffi: mejor huella TLS; suele recuperar 403 tipo Vunesp/IBFC.
  3) Playwright: navegador real para Cloudflare/challenges/JS pesado.

Instalacion recomendada en tu Mac:
  /usr/bin/python3 -m pip install --user curl_cffi playwright

Si no tienes Chrome instalado o quieres Chromium de Playwright:
  /usr/bin/python3 -m playwright install chromium

Ejecucion tipica:
  /usr/bin/python3 bancas_fetcher_local_v2.py --headful

Solo reintentar los fallidos de una salida anterior:
  /usr/bin/python3 bancas_fetcher_local_v2.py --retry-failed-from ~/test_bancas_local.xlsx --headful
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from excel_utils import read_table, write_table

warnings.filterwarnings(
    "ignore",
    message=r".*urllib3 v2 only supports OpenSSL.*",
    category=Warning,
)
warnings.filterwarnings("ignore", message=r".*Unverified HTTPS request.*", category=Warning)

try:
    import requests as rq
except Exception as exc:  # pragma: no cover - requests should exist on this Mac
    rq = None
    REQUESTS_IMPORT_ERROR = exc
else:
    REQUESTS_IMPORT_ERROR = None
    try:
        rq.packages.urllib3.disable_warnings()
    except Exception:
        pass

try:
    from curl_cffi import requests as creq
except Exception as exc:
    creq = None
    CURL_IMPORT_ERROR = exc
else:
    CURL_IMPORT_ERROR = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception as exc:
    sync_playwright = None
    PlaywrightTimeoutError = Exception
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None


BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7,es;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

HARD_CHALLENGE_MARKERS = [
    "just a moment",
    "cf-chl",
    "cf_chl",
    "cf-challenge",
    "cf-browser-verification",
    "checking your browser",
    "checking if the site connection is secure",
    "attention required",
    "cloudflare ray id",
    "error 1020",
    "you don't have permission to access",
    "request unsuccessful",
    "enable javascript and cookies",
    "verificando se voce",
    "verificando seu navegador",
]

SOFT_PROTECTION_MARKERS = [
    "challenge-platform",
    "/cdn-cgi/challenge-platform",
    "turnstile",
    "captcha",
    "g-recaptcha",
    "hcaptcha",
    "incapsula",
    "akamai",
    "akamai bot manager",
    "ddos-guard",
]

CHALLENGE_TITLES = [
    "access denied",
    "attention required",
    "forbidden",
    "just a moment",
    "not allowed",
    "request blocked",
]

SOURCES = [
    ("fundatec", "Fundatec", "https://fundatec.org.br/", "banca"),
    ("fgv", "FGV Conhecimento", "https://conhecimento.fgv.br/concursos", "banca"),
    ("cesgranrio", "Cesgranrio", "https://www.cesgranrio.org.br/", "banca"),
    ("vunesp", "Vunesp", "https://www.vunesp.com.br/", "banca"),
    ("quadrix", "Quadrix", "https://www.quadrix.org.br/", "banca"),
    ("ibfc", "IBFC", "https://www.ibfc.org.br/", "banca"),
    ("consulplan", "Instituto Consulplan", "https://www.institutoconsulplan.org.br/", "banca"),
    ("access", "Instituto ACCESS", "https://access.org.br/", "banca"),
    ("objetiva", "Objetiva Concursos", "https://www.objetivas.com.br/", "banca"),
    ("lasalle", "FundaÃ§Ã£o La Salle", "https://fundacaolasalle.org.br/concursos/", "banca"),
    ("faurgs", "FAURGS", "https://portalfaurgs.com.br/", "banca"),
    ("avancasp", "Avança SP", "https://www.avancasp.org.br/", "banca"),
    (
        "doe_ce",
        "Diário Oficial do CE",
        "http://pesquisa.doe.seplag.ce.gov.br/doepesquisa/sead.do?action=Ultimas&cmd=11&page=ultimasEdicoes",
        "diario",
    ),
    ("dodf", "Diário Oficial do DF", "https://dodf.df.gov.br/", "diario"),
    ("prf", "Polícia Rodoviária Federal", "https://www.gov.br/prf/pt-br", "orgao"),
    ("estrategia", "Estratégia Concursos", "https://www.estrategiaconcursos.com.br/blog/", "radar"),
    ("pref_floripa", "Prefeitura de Florianópolis", "https://www.pmf.sc.gov.br/", "prefeitura"),
    ("pref_maringa", "Prefeitura de Maringá", "https://www2.maringa.pr.gov.br/", "prefeitura"),
]

GOOD_RESULTS = {"easy", "js"}
RETRY_RESULTS = {"hostile", "challenge", "retry", "error", "js"}


@dataclass
class Source:
    source_id: str
    name: str
    url: str
    kind: str


@dataclass
class FetchResult:
    engine: str
    result: str
    note: str
    status: Optional[int] = None
    final_url: str = ""
    title: str = ""
    visible_chars: int = 0
    elapsed_s: float = 0.0
    body: str = ""
    error: str = ""
    saved_html: str = ""
    screenshot: str = ""


def visible_text(html: str) -> str:
    html = html or ""
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def visible_len(html: str) -> int:
    return len(visible_text(html))


def page_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title[:180]


def classify(status: Optional[int], body: str, content_type: str = "", error: str = "") -> Tuple[str, str, int]:
    if error:
        return "error", "err:" + error, 0

    body = body or ""
    low = body.lower()
    text = visible_text(body)
    front = text[:700].lower()
    vlen = len(text)
    title = page_title(body).lower()
    link_count = len(re.findall(r"(?i)<a\b", body))
    content_type = (content_type or "").lower()

    if "application/pdf" in content_type or body[:8].startswith("%PDF"):
        return "easy", "pdf", max(vlen, len(body))

    challenge_title = any(marker in title for marker in CHALLENGE_TITLES)
    denied_text = front.startswith("access denied") or "you don't have permission to access" in front
    has_hard_challenge = any(marker in low for marker in HARD_CHALLENGE_MARKERS)
    has_soft_protection = any(marker in low for marker in SOFT_PROTECTION_MARKERS)
    looks_loaded = (
        not challenge_title
        and not denied_text
        and (
            vlen >= 1000
            or (vlen >= 500 and (bool(title) or link_count >= 8))
        )
    )

    if status in (401, 403, 407, 451):
        if looks_loaded:
            return "easy", f"ok_text{vlen}_http_{status}", vlen
        return (
            ("challenge", "challenge", vlen)
            if has_hard_challenge
            else ("hostile", f"http_{status}", vlen)
        )

    if status in (429, 503):
        if looks_loaded and not has_hard_challenge:
            return "easy", f"ok_text{vlen}_http_{status}", vlen
        return (
            ("challenge", "challenge", vlen)
            if has_hard_challenge
            else ("retry", f"http_{status}", vlen)
        )

    if has_hard_challenge and not looks_loaded:
        return "challenge", "challenge", vlen

    if status is not None and status >= 400:
        if looks_loaded:
            return "easy", f"ok_text{vlen}_http_{status}", vlen
        return "hostile", f"http_{status}", vlen

    if vlen < 200:
        return "js", f"lowtext{vlen}", vlen

    if has_soft_protection:
        return "easy", f"ok_text{vlen}_protection_asset", vlen

    return "easy", f"ok_text{vlen}", vlen


def safe_stem(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", text.strip())
    return text.strip("_") or "item"


def should_save_html(policy: str, result: FetchResult) -> bool:
    if policy == "none":
        return False
    if policy == "all":
        return bool(result.body)
    return bool(result.body) and result.result not in GOOD_RESULTS


def save_html_if_needed(outdir: Path, source: Source, result: FetchResult, policy: str) -> None:
    if not should_save_html(policy, result):
        return
    raw_dir = outdir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{source.source_id}_{safe_stem(result.engine)}_{result.result}.html"
    path.write_text(result.body, encoding="utf-8", errors="replace")
    result.saved_html = str(path)


def fetch_with_requests(source: Source, timeout: int, verify_ssl: bool, retries: int) -> FetchResult:
    if rq is None:
        return FetchResult("requests", "error", "missing_requests", error=str(REQUESTS_IMPORT_ERROR))

    session = rq.Session()
    session.headers.update(BASE_HEADERS)
    last_error = ""
    started = time.monotonic()

    for attempt in range(retries + 1):
        try:
            resp = session.get(source.url, timeout=timeout, allow_redirects=True, verify=verify_ssl)
            body = resp.text or ""
            result, note, vlen = classify(resp.status_code, body, resp.headers.get("content-type", ""))
            return FetchResult(
                engine="requests",
                result=result,
                note=note,
                status=resp.status_code,
                final_url=resp.url,
                title=page_title(body),
                visible_chars=vlen,
                elapsed_s=time.monotonic() - started,
                body=body,
            )
        except Exception as exc:
            last_error = type(exc).__name__
            if attempt < retries:
                time.sleep(1.0 + attempt)

    return FetchResult(
        engine="requests",
        result="error",
        note="err:" + last_error,
        elapsed_s=time.monotonic() - started,
        error=last_error,
    )


def fetch_with_curl_cffi(source: Source, timeout: int, verify_ssl: bool, retries: int) -> FetchResult:
    if creq is None:
        return FetchResult("curl_cffi", "error", "missing_curl_cffi", error=str(CURL_IMPORT_ERROR))

    started = time.monotonic()
    imps = ["chrome124", "chrome120", "chrome110", "chrome101", "chrome"]
    last_error = ""

    for attempt in range(retries + 1):
        for imp in imps:
            try:
                resp = creq.get(
                    source.url,
                    impersonate=imp,
                    headers={"Accept-Language": BASE_HEADERS["Accept-Language"]},
                    timeout=timeout,
                    allow_redirects=True,
                    verify=verify_ssl,
                )
                body = resp.text or ""
                result, note, vlen = classify(resp.status_code, body, resp.headers.get("content-type", ""))
                return FetchResult(
                    engine=f"curl_cffi:{imp}",
                    result=result,
                    note=note,
                    status=resp.status_code,
                    final_url=resp.url,
                    title=page_title(body),
                    visible_chars=vlen,
                    elapsed_s=time.monotonic() - started,
                    body=body,
                )
            except ValueError as exc:
                last_error = type(exc).__name__
                continue
            except Exception as exc:
                last_error = type(exc).__name__
                break
        if attempt < retries:
            time.sleep(1.0 + attempt)

    return FetchResult(
        engine="curl_cffi",
        result="error",
        note="err:" + last_error,
        elapsed_s=time.monotonic() - started,
        error=last_error,
    )


class PlaywrightFetcher:
    def __init__(
        self,
        timeout: int,
        verify_ssl: bool,
        headful: bool,
        browser_channel: str,
        manual_wait: int,
        settle_ms: int,
        screenshots: bool,
        outdir: Path,
    ) -> None:
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.headful = headful
        self.browser_channel = browser_channel.strip()
        self.manual_wait = manual_wait
        self.settle_ms = settle_ms
        self.screenshots = screenshots
        self.outdir = outdir
        self._pw = None
        self._browser = None
        self._context = None

    def available(self) -> bool:
        return sync_playwright is not None

    def start(self) -> None:
        if self._context is not None:
            return
        if sync_playwright is None:
            raise RuntimeError("playwright_missing")

        self._pw = sync_playwright().start()
        launch_kwargs = {
            "headless": not self.headful,
        }
        if self.browser_channel:
            launch_kwargs["channel"] = self.browser_channel

        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception:
            if self.browser_channel:
                launch_kwargs.pop("channel", None)
                self._browser = self._pw.chromium.launch(**launch_kwargs)
            else:
                raise

        self._context = self._browser.new_context(
            user_agent=BASE_HEADERS["User-Agent"],
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1365, "height": 900},
            ignore_https_errors=not self.verify_ssl,
            extra_http_headers={
                "Accept-Language": BASE_HEADERS["Accept-Language"],
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )

    def close(self) -> None:
        for obj in (self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._pw = None

    def fetch(self, source: Source) -> FetchResult:
        if sync_playwright is None:
            return FetchResult("playwright", "error", "missing_playwright", error=str(PLAYWRIGHT_IMPORT_ERROR))

        started = time.monotonic()
        page = None
        try:
            self.start()
            page = self._context.new_page()
            page.set_default_timeout(self.timeout * 1000)
            response = page.goto(source.url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            status = response.status if response is not None else None
            final_url = page.url

            try:
                page.wait_for_load_state("networkidle", timeout=min(12_000, self.timeout * 1000))
            except Exception:
                pass
            if self.settle_ms > 0:
                page.wait_for_timeout(self.settle_ms)

            body = page.content()
            result, note, vlen = classify(status, body)

            if result == "challenge" and self.headful and self.manual_wait > 0:
                print(f"      Playwright detecto challenge en {source.name}. Si aparece captcha, resuelvelo; espero {self.manual_wait}s.")
                page.wait_for_timeout(self.manual_wait * 1000)
                body = page.content()
                result, note, vlen = classify(status, body)

            title = ""
            try:
                title = page.title()[:180]
            except Exception:
                title = page_title(body)

            screenshot_path = ""
            if self.screenshots:
                shot_dir = self.outdir / "screenshots"
                shot_dir.mkdir(parents=True, exist_ok=True)
                shot_path = shot_dir / f"{source.source_id}_playwright_{result}.png"
                page.screenshot(path=str(shot_path), full_page=True)
                screenshot_path = str(shot_path)

            return FetchResult(
                engine="playwright",
                result=result,
                note=note,
                status=status,
                final_url=final_url,
                title=title,
                visible_chars=vlen,
                elapsed_s=time.monotonic() - started,
                body=body,
                screenshot=screenshot_path,
            )
        except PlaywrightTimeoutError:
            return FetchResult(
                engine="playwright",
                result="error",
                note="err:Timeout",
                elapsed_s=time.monotonic() - started,
                error="Timeout",
            )
        except Exception as exc:
            return FetchResult(
                engine="playwright",
                result="error",
                note="err:" + type(exc).__name__,
                elapsed_s=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass


def load_sources(retry_failed_from: str, include_js: bool) -> List[Source]:
    sources = [Source(*row) for row in SOURCES]
    if not retry_failed_from:
        return sources

    failed_ids = set()
    path = Path(os.path.expanduser(retry_failed_from))
    for row in read_table(path):
        result = (row.get("resultado") or row.get("final_result") or "").strip()
        sid = (row.get("source_id") or "").strip()
        if not sid:
            continue
        if result not in GOOD_RESULTS or (include_js and result == "js"):
            failed_ids.add(sid)

    return [src for src in sources if src.source_id in failed_ids]


def parse_engines(raw: str) -> List[str]:
    if raw.strip().lower() == "all":
        return ["requests", "curl", "playwright"]
    engines = []
    for item in raw.split(","):
        item = item.strip().lower()
        if item == "curl_cffi":
            item = "curl"
        if item in {"requests", "curl", "playwright"} and item not in engines:
            engines.append(item)
    return engines or ["requests", "curl", "playwright"]


def print_doctor(engines: Sequence[str]) -> None:
    print("Motores disponibles:")
    print(f"  requests   : {'OK' if rq is not None else 'FALTA'}")
    print(f"  curl_cffi  : {'OK' if creq is not None else 'FALTA'}")
    print(f"  playwright : {'OK' if sync_playwright is not None else 'FALTA'}")
    missing = []
    if "curl" in engines and creq is None:
        missing.append("curl_cffi")
    if "playwright" in engines and sync_playwright is None:
        missing.append("playwright")
    if missing:
        print("\nPara activar los motores que faltan:")
        print("  /usr/bin/python3 -m pip install --user curl_cffi playwright")
        print("  /usr/bin/python3 -m playwright install chromium   # solo si no usas Chrome del sistema")


def should_try_next(result: FetchResult, playwright_for_js: bool) -> bool:
    if result.result in {"easy"}:
        return False
    if result.result == "js" and not playwright_for_js:
        return False
    return result.result in RETRY_RESULTS


def run_source(
    source: Source,
    engines: Sequence[str],
    args: argparse.Namespace,
    browser: PlaywrightFetcher,
    outdir: Path,
) -> Tuple[FetchResult, Dict[str, FetchResult]]:
    attempts: Dict[str, FetchResult] = {}

    if "requests" in engines:
        res = fetch_with_requests(source, args.timeout, args.verify_ssl, args.retries)
        save_html_if_needed(outdir, source, res, args.save_html)
        attempts["requests"] = res
        if not should_try_next(res, args.playwright_for_js):
            return res, attempts

    if "curl" in engines:
        res = fetch_with_curl_cffi(source, args.timeout, args.verify_ssl, args.retries)
        save_html_if_needed(outdir, source, res, args.save_html)
        attempts["curl"] = res
        if not should_try_next(res, args.playwright_for_js):
            return res, attempts

    if "playwright" in engines:
        res = browser.fetch(source)
        save_html_if_needed(outdir, source, res, args.save_html)
        attempts["playwright"] = res
        return res, attempts

    if attempts:
        return list(attempts.values())[-1], attempts
    return FetchResult("none", "error", "no_engine"), attempts


def csv_row(source: Source, final: FetchResult, attempts: Dict[str, FetchResult]) -> Dict[str, object]:
    row: Dict[str, object] = {
        "source_id": source.source_id,
        "source_name": source.name,
        "tipo": source.kind,
        "url": source.url,
        "final_result": final.result,
        "final_note": final.note,
        "final_engine": final.engine,
        "final_status": final.status if final.status is not None else "",
        "final_url": final.final_url,
        "title": final.title,
        "visible_chars": final.visible_chars,
        "elapsed_s": round(final.elapsed_s, 2),
        "saved_html": final.saved_html,
        "screenshot": final.screenshot,
        "error": final.error,
    }
    for key in ("requests", "curl", "playwright"):
        res = attempts.get(key)
        row[f"{key}_result"] = res.result if res else ""
        row[f"{key}_note"] = res.note if res else ""
        row[f"{key}_status"] = res.status if res and res.status is not None else ""
        row[f"{key}_engine"] = res.engine if res else ""
    return row


def summarize(rows: List[Dict[str, object]]) -> None:
    total = len(rows)
    ok = [r for r in rows if r["final_result"] in GOOD_RESULTS]
    bancas = [r for r in rows if r["tipo"] == "banca"]
    bancas_ok = [r for r in bancas if r["final_result"] in GOOD_RESULTS]
    by_engine: Dict[str, int] = {}
    pending: List[str] = []

    for row in rows:
        if row["final_result"] in GOOD_RESULTS:
            by_engine[str(row["final_engine"])] = by_engine.get(str(row["final_engine"]), 0) + 1
        else:
            pending.append(f"{row['source_name']} ({row['final_note']})")

    print("\n=================== RESULTADO V2 ===================")
    print(f"  Abrieron/parseables: {len(ok)}/{total} fuentes")
    print(f"  Bancas parseables  : {len(bancas_ok)}/{len(bancas)}")
    if by_engine:
        print("  Recuperadas por motor:")
        for engine, count in sorted(by_engine.items(), key=lambda item: item[0]):
            print(f"    - {engine}: {count}")
    if pending:
        print("\n  Pendientes:")
        for item in pending:
            print(f"    - {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tester local v2 para bancas y fuentes bloqueadas.")
    parser.add_argument("--engines", default="all", help="all, requests, curl, playwright o lista coma-separada.")
    parser.add_argument("--retry-failed-from", default="", help="CSV/XLSX anterior: reintenta solo fuentes fallidas.")
    parser.add_argument("--include-js", action="store_true", help="Con --retry-failed-from, tambien reintenta lowtext/js.")
    parser.add_argument("--outdir", default="", help="Carpeta de salida. Default: bancas_fetch_v2_YYYYmmdd_HHMMSS.")
    parser.add_argument("--timeout", type=int, default=35, help="Timeout por request/navegacion, en segundos.")
    parser.add_argument("--retries", type=int, default=1, help="Reintentos por motor HTTP.")
    parser.add_argument("--delay-min", type=float, default=2.0, help="Pausa minima entre fuentes.")
    parser.add_argument("--delay-max", type=float, default=3.5, help="Pausa maxima entre fuentes.")
    parser.add_argument("--verify-ssl", action="store_true", help="Verifica certificados SSL. Default: no verificar.")
    parser.add_argument("--headful", action="store_true", help="Abre navegador visible para Cloudflare/captcha.")
    parser.add_argument("--browser-channel", default="chrome", help="Canal Playwright: chrome, chromium, msedge, o vacio.")
    parser.add_argument("--manual-wait", type=int, default=45, help="Segundos de espera si Playwright ve challenge.")
    parser.add_argument("--settle-ms", type=int, default=2500, help="Espera extra despues de cargar la pagina.")
    parser.add_argument("--screenshots", action="store_true", help="Guarda screenshots de Playwright.")
    parser.add_argument("--save-html", choices=["failed", "all", "none"], default="failed", help="Guardar HTML bruto.")
    parser.add_argument("--playwright-for-js", action="store_true", help="Usa Playwright tambien para lowtext/js.")
    parser.add_argument("--doctor", action="store_true", help="Solo muestra motores instalados y comandos utiles.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engines = parse_engines(args.engines)
    if args.doctor:
        print_doctor(engines)
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path(args.outdir or f"bancas_fetch_v2_{timestamp}").expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    print_doctor(engines)
    sources = load_sources(args.retry_failed_from, args.include_js)
    print(f"\nProbando {len(sources)} fuentes desde esta Mac. NordVPN apagado para medir tu IP real.")
    print(f"Salida: {outdir}\n")

    browser = PlaywrightFetcher(
        timeout=args.timeout,
        verify_ssl=args.verify_ssl,
        headful=args.headful,
        browser_channel=args.browser_channel,
        manual_wait=args.manual_wait,
        settle_ms=args.settle_ms,
        screenshots=args.screenshots,
        outdir=outdir,
    )

    rows: List[Dict[str, object]] = []
    try:
        for idx, source in enumerate(sources, start=1):
            final, attempts = run_source(source, engines, args, browser, outdir)
            row = csv_row(source, final, attempts)
            rows.append(row)

            mark = "[OK]" if final.result == "easy" else ("[JS]" if final.result == "js" else "[!!]")
            print(
                f"  {mark} {idx:02d}/{len(sources):02d} "
                f"{source.name[:32]:32s} {final.result:10s} "
                f"via {final.engine[:18]:18s} ({final.note})"
            )

            if idx < len(sources):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
    finally:
        browser.close()

    out_path = outdir / "test_bancas_local_v2.xlsx"
    fieldnames = list(csv_row(Source("x", "x", "x", "x"), FetchResult("x", "x", "x"), {}).keys())
    out_path = write_table(rows, fieldnames, out_path, sheet_name="Fase 1")

    summarize(rows)
    print(f"\n  Detalle guardado en: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
