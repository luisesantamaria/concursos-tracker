import sys, re
from pathlib import Path
from urllib.parse import urljoin, urlparse
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fase1_v1 as f1

URL = "https://www.acheconcursos.com.br/concursos-rio-grande-do-sul"
fs = f1.Source("ache", "ache", URL, "radar")
res = f1.fetch_with_requests(fs, 30, False, 1)
print("status", res.status, "result", res.result, "chars", len(res.body or ""))

html = res.body or ""
pairs = re.findall(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S)
abss = []
for href, anchor in pairs:
    anchor = re.sub(r"<[^>]+>", " ", anchor)
    anchor = re.sub(r"\s+", " ", anchor).strip()
    absu = urljoin(URL, href)
    abss.append((absu, anchor))

host = "acheconcursos.com.br"
internal = [(u, a) for u, a in abss if host in urlparse(u).netloc]
# concurso detail pages typically: /concursos-<uf>/<slug> or /concurso-...
print("\ntotal links:", len(abss), "internal:", len(internal))

# show a sample of internal links that look like concurso detail
detail = [(u, a) for u, a in internal if "/concurso" in u.lower()]
print("internal con '/concurso':", len(detail))
from collections import Counter
# show URL path patterns
pats = Counter()
for u, a in detail:
    p = urlparse(u).path
    seg = "/".join(p.strip("/").split("/")[:1])
    pats[seg] += 1
print("primeros segmentos de path:")
for k, v in pats.most_common(15):
    print(f"  /{k}/  x{v}")

print("\nmuestra 30 detail links:")
for u, a in detail[:30]:
    print(f"  {a[:55]:55s} {u}")
