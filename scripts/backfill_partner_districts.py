"""Vienreizējs: agent_partner listingiem bez rajona uzmin rajonu (tuvākie mūsu listingi ar koordinātām) un ieraksta
listingā + ēkas profilā; publicētos pārpublicē. Palaiž zaļā pēc workera deploja:
    DATABASE_URL=… GOOGLE_MAPS_API_KEY=… python -P scripts/backfill_partner_districts.py [--apply]
Bez --apply tikai parāda, ko darītu. (2026-10-27: Eften Ieriķu 3 → VEF, Jūrkalnes 15/25 → Šampēteris-Pleskodāle,
Mūkusalas 42D → Torņakalns; ārpus Rīgas rajonu neliek.)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import partner_intake as pi  # noqa: E402

apply = "--apply" in sys.argv
conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
rows = conn.execute("""SELECT id, street, city, building_profile_id bp, wp_post_id wp FROM properties.listings
                       WHERE source='agent_partner' AND nullif(btrim(coalesce(district,'')),'') IS NULL ORDER BY id""").fetchall()
for r in rows:
    d = pi._guess_district(conn, r["street"], r["city"], r["bp"])
    print(f"{r['id']:>7} {r['street']}, {r['city']} → {d or '(nav)'}")
    if not d or not apply:
        continue
    conn.execute("UPDATE properties.listings SET district=%s WHERE id=%s", (d, r["id"]))
    if r["bp"]:
        conn.execute("UPDATE properties.building_profiles SET district=%s WHERE id=%s AND coalesce(btrim(district),'')=''",
                     (d, r["bp"]))
    if r["wp"]:
        conn.execute("""INSERT INTO properties.wp_export_queue (listing_id, status, requested_by)
                        VALUES (%s,'pending','backfill_districts')
                        ON CONFLICT (listing_id) WHERE status IN ('pending','processing') DO NOTHING""", (r["id"],))
if apply:
    conn.commit()
    print("ierakstīts + pārpublicēšana ierindota")
else:
    print("(dry run — pievieno --apply)")
