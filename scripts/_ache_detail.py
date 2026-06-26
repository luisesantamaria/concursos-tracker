import sys, re
from pathlib import Path
from urllib.parse import urljoin, urlparse
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fase1_v1 as f1

LIST = "https://www.acheconcursos.com.br/concursos-rio-grande-do-sul"
res = f1.fetch_with_requests(f1.Source("a","a",LIST,"r"), 30, False, 1)
html = res.body or ""

# detail links
details = []
seen = set()
for m in re.finditer(r'href\s*=\s*["\'](/concursos-rio-grande-do-sul/[^"\']+)["\']', html, re.I):
    href = m.group(1)
    slug = href[len("/concursos-rio-grande-do-sul/"):].strip("/")
    if not slug or "/" in slug:
        continue
    u = urljoin("https://www.acheconcursos.com.br/", href)
    if u in seen: continue
    seen.add(u)
    details.append(u)

print("primeros 6 detalle URLs:")
for u in details[:6]:
    print("  ", u)

# fetch primero que de 200 y dump
target = None
for u in details[:6]:
    d = f1.fetch_with_requests(f1.Source("a","a",u,"r"), 30, False, 1)
    print(f"  status {d.status} chars {len(d.body or '')}  {u}")
    if d.status == 200 and not target:
        target = (u, d.body or "")

if target:
    u, h = target
    print("\n=== DETALLE 200:", u)
    print("title:", f1.page_title(h))
    # external links
    print("\nexternos no-social:")
    for m in re.finditer(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', h, re.I|re.S):
        href = m.group(1).strip()
        anchor = re.sub(r"<[^>]+>"," ",m.group(2)); anchor=re.sub(r"\s+"," ",anchor).strip()
        absu = urljoin(u, href)
        host = urlparse(absu).netloc.lower()
        if not host or "acheconcursos" in host: continue
        if any(s in host for s in ("facebook","instagram","x.com","t.me","desenvolveweb","whatsapp","youtube","google")): continue
        print(f"   {host:30s} | {anchor[:45]:45s} | {absu[:75]}")
    # edital / baixar context
    print("\ncontexto 'edital'/'baixar'/'acesse':")
    for kw in ("baixar edital","edital de abertura","acesse o edital","clique aqui","site oficial","leia o edital","edital"):
        i = h.lower().find(kw)
        if i>=0:
            frag = re.sub(r"\s+"," ", h[max(0,i-150):i+200])
            print(f"   [{kw}] ...{frag}...")
