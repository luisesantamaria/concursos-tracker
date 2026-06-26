import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from excel_utils import read_xlsx_dicts

p = r"C:\Users\Luis Santamaria\.claude\uploads\e7fd6e26-ec4d-470a-b95d-857b9717aeda\87d4e102-Concursos_RS__Fase_2D_integrada_20260623_v3.xlsx"
rows = read_xlsx_dicts(p)
print("TOTAL ROWS:", len(rows))
if rows:
    print("COLUMNS:", list(rows[0].keys()))
    print("=" * 70)
    for r in rows[:3]:
        for k, v in r.items():
            sv = str(v)
            if len(sv) > 80:
                sv = sv[:80] + "..."
            print(f"  {k}: {sv}")
        print("-" * 50)
