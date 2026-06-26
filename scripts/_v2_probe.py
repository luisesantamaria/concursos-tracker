import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import argparse
import ache_rs_official_pipeline as a

ns = argparse.Namespace(timeout=30, cache=True, delay_min=0.0, delay_max=0.0, resolve_min_score=8)
res = a.fetch("https://concursos.objetivas.com.br/", ns)
print("status", res.status, "len", len(res.body or ""))
cands = a.candidate_links_from_index(res.body or "", res.final_url or "https://concursos.objetivas.com.br/")
spec = [(u, c[:60]) for u, c in cands if a.is_specific_official_url(u)]
print("total_cands", len(cands), "specific", len(spec))
for u, c in spec[:8]:
    print("  ", u, "::", c)
