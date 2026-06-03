"""Ātrs render_body tests pa listing id (izvelk listing+bp no DB).
Lietošana: python _sim_tc.py <listing_id>
"""
import os, re, sys
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(Path(__file__).resolve().parents[1] / "sslv-ai-runner-railway" / "crm" / ".env")

from wp_templates import render_body

lid = int(sys.argv[1])
with psycopg.connect(os.getenv("DATABASE_URL"), row_factory=dict_row) as c:
    L = c.execute("select * from properties.listings where id=%s", (lid,)).fetchone()
    bp = None
    if L and L.get("building_profile_id"):
        bp = c.execute("select * from properties.building_profiles where id=%s",
                       (L["building_profile_id"],)).fetchone()

sg = (L.get("Space_group") or "").strip()
print(f"--- listing {lid} | sg={sg!r} | btype={(bp or {}).get('building_type')!r} | "
      f"name={(bp or {}).get('building_name')!r} | floor={L.get('floor')!r} ---\n")
html = render_body(sg, L, bp)
text = re.sub(r"<[^>]+>", "", html.replace("</p>", "\n").replace("<br>", "\n"))
text = re.sub(r"\n{2,}", "\n\n", text).strip()
print(text)
