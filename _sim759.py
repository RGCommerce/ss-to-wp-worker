"""Simulācija: 759 ar atjaunoto ēkas info (Lubānas Biznesa centrs)."""
import os
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
load_dotenv(Path("..").resolve() / "sslv-ai-runner-railway" / "crm" / ".env")
import _proto_render as P  # tas pats uzstāda utf-8 stdout

with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
    L = conn.execute("SELECT * FROM properties.listings WHERE id=759").fetchone()
    bp = {}
    if L.get("building_profile_id"):
        bp = conn.execute("SELECT * FROM properties.building_profiles WHERE id=%s",
                          (L["building_profile_id"],)).fetchone() or {}

# Raimonda ievadītā ēkas info (simulācija — kā tas būtu DB pēc atjaunošanas):
bp.update({
    "building_name": "Lubānas Biznesa centrs",
    "floors_count": "6",
    "has_conference_room": "checked",
    "has_reception": "checked",
    "has_roof_terrace": "checked",
    "has_canteen": "checked",
    "ednica_nosaukums": "Ozoli",
    "has_accessibility": "checked",
})
print(P.to_text(P.render(L, bp)))
