"""Vienreizējs: esošie PDF/manuālie importi (source agent_pdf/agent_manual) → AI klasifikators.
Katrai rindai: original_text no avota PDF (source_file_path, STORAGE_ROOT), un, ja ir bildes
(local_image_paths_processed), Debug_status=NULL → agent_ai_poller to paņem (pollera filtrā jābūt agent_pdf/agent_manual).
Palaiž zaļā uz workera (volume): python -P scripts/backfill_pdf_imports_ai.py [--apply]"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import pdf_import  # noqa: E402

apply = "--apply" in sys.argv
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/storage"))
conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
rows = conn.execute("""SELECT id, street, source_file_path, original_text, "Debug_status" AS ds,
                              coalesce(array_length(local_image_paths_processed,1),0) AS n_img
                         FROM properties.listings WHERE source IN ('agent_pdf','agent_manual') ORDER BY id""").fetchall()
for r in rows:
    txt = None
    if r["source_file_path"]:
        p = STORAGE_ROOT / r["source_file_path"]
        if p.is_file():
            try:
                txt, _ = pdf_import.extract_text(p)
            except Exception as e:
                txt = None
                print(f"  ! {r['id']}: PDF teksts neizdevās: {e}")
    will_ai = r["n_img"] > 0
    print(f"{r['id']:>7} {r['street']} | bildes {r['n_img']} | teksts {len(txt or '')} z. | tagad {r['ds']} → "
          f"{'NULL (AI)' if will_ai else 'paliek (nav bilžu)'}")
    if not apply:
        continue
    if txt and txt.strip() and not (r["original_text"] or "").strip():
        conn.execute("UPDATE properties.listings SET original_text = %s WHERE id = %s", (txt[:20000], r["id"]))
    if will_ai:
        conn.execute("""UPDATE properties.listings SET "Debug_status" = NULL WHERE id = %s""", (r["id"],))
if apply:
    conn.commit()
    print("ierakstīts")
else:
    print("(dry run — pievieno --apply)")
