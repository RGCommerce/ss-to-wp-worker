"""Vienreizējais backfils: iztulko VISUS mājaslapas sludinājumus EN+RU.

Ņem visus on_website listingus ar wp_post_id, katram paņem WP aprakstu
(rgc_catalog_detail) un izlaiž caur translate.translate_listing — kešs
un hash nodrošina, ka jau tulkotais netiek tulkots vēlreiz, tāpēc skriptu
DROŠI var pārtraukt un palaist atkārtoti (turpinās, kur palika).

Tas pats skripts der arī kā REZERVES fona darbs (piem. reizi naktī):
atkārtota palaišana pārtulko tikai jauno/mainīto — «bez mūsu iesaistes».

Palaišana:
    python backfill_translations.py               # viss, secīgi
    python backfill_translations.py --limit 50    # pirmie 50 (tests)
    python backfill_translations.py --workers 8   # paralēli (pilnajam backfilam)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from translate import _db, translate_listing, ensure_table

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PAUSE = float(os.getenv("TR_PAUSE", "0.4"))  # sek starp listingiem (saudzē WP/OpenAI)


def main() -> None:
    limit = 0
    workers = 1
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--workers" in sys.argv:
        workers = max(1, int(sys.argv[sys.argv.index("--workers") + 1]))

    conn = _db()
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT wp_post_id FROM properties.listings "
            "WHERE on_website = true AND wp_post_id IS NOT NULL "
            "ORDER BY wp_post_id DESC" + (f" LIMIT {limit}" if limit else ""))
        ids = [r[0] for r in cur.fetchall()]
    conn.close()

    print(f"Backfils: {len(ids)} sludinājumi → EN+RU (workers={workers})", flush=True)
    tot = {"translated": 0, "cached": 0, "kept": 0, "failed": 0, "empty": 0}
    done = {"n": 0}
    lock = threading.Lock()
    t0 = time.time()

    def one(wp_id) -> None:
        st = translate_listing(wp_id)
        with lock:
            if st is None:
                tot["empty"] += 1
            else:
                for k in ("translated", "cached", "kept", "failed"):
                    tot[k] += st[k]
            done["n"] += 1
            if done["n"] % 25 == 0 or done["n"] == len(ids):
                mins = (time.time() - t0) / 60
                print(f"  {done['n']}/{len(ids)} ({mins:.1f} min) — "
                      f"tulkots {tot['translated']}, kešs {tot['cached']}, "
                      f"nemainīts {tot['kept']}, kļūdas {tot['failed']}, "
                      f"tukši {tot['empty']}", flush=True)
        if workers == 1 and st and (st["translated"] or st["cached"]):
            time.sleep(PAUSE)

    if workers == 1:
        for wp_id in ids:
            one(wp_id)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, ids))
    print("GATAVS:", tot, flush=True)


if __name__ == "__main__":
    main()
