import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import argparse
import ache_rs_official_pipeline as ache
import fase1_v1 as f1

ns = argparse.Namespace(timeout=30, cache=True, delay_min=0.0, delay_max=0.0, resolve_min_score=8)
urls = [
    "https://www.sinimbu.rs.gov.br/Lista/3804/Concursos-Publicos",
    "https://www.santoantoniodapatrulha.rs.gov.br/concurso-publico/",
]
for u in urls:
    res = ache.fetch(u, ns)
    text = f1.visible_text(res.body or "") if res.body else ""
    norm = ache.normalize_text(text[:40000])
    has_conc = "concurso" in norm
    has_edital = "edital" in norm
    print("=" * 80)
    print(u)
    print("  status", res.status, "| final", res.final_url, "| len", len(res.body or ""))
    print("  visible 'concurso':", has_conc, "| 'edital':", has_edital)
    # show first lines mentioning edital/concurso
    for line in text.splitlines():
        ln = ache.normalize_text(line)
        if ("edital" in ln or "concurso" in ln) and len(line.strip()) > 5:
            print("   >", line.strip()[:110])
