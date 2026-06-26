import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import read_csv_dicts

rows = read_csv_dicts(Path(__file__).resolve().parent.parent / "data" / "ache_rs_fase2_v2.csv")
for r in rows:
    if (r.get("v2_status") or "") in {"resolved", "home"}:
        print("=" * 80)
        print("n", r["n"], "|", r["status"], "|", r["orgao"])
        print("  strategy:", r["v2_strategy"], "| method:", r["v2_method"], "| score:", r["v2_score"])
        print("  official:", r["v2_official_url"])
        print("  docs    :", r["v2_doc_urls"])
