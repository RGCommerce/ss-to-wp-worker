"""Vienreizējais backfils: iztulko VISUS mājaslapas sludinājumus EN+RU.

Ņem visus on_website listingus ar wp_post_id, katram paņem WP aprakstu
(rgc_catalog_detail) un izlaiž caur translate.translate_listing — kešs
un hash nodrošina, ka jau tulkotais netiek tulkots vēlreiz, tāpēc skriptu
DROŠI var pārtraukt un palaist atkārtoti (turpinās, kur palika).

Tas pats skripts der arī kā REZERVES fona darbs (piem. reizi naktī):
atkārtota palaišana pārtulko tikai jauno/mainīto — «bez mūsu iesaistes».

Palaišana:
    python backfill_translations.py            # viss (ar pauzēm, lai netraucē WP)
    python backfill_translations.py --limit 50 # pirmie 50 (tests)
"""
from __future__ import annotations

import os
import sys
import time

from translate import _db, translate_listing, ensure_table

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PAUSE = float(os.getenv("TR_PAUSE", "0.4"))  # sek starp listingiem (saudzē WP/OpenAI)


def main() -> None:
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    conn = _db()
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT wp_post_id FROM properties.listings "
            "WHERE on_website = true AND wp_post_id IS NOT NULL "
            "ORDER BY wp_post_id DESC" + (f" LIMIT {limit}" if limit else ""))
        ids = [r[0] for r in cur.fetchall()]
    conn.close()

    print(f"Backfils: {len(ids)} sludinājumi → EN+RU")
    tot = {"translated": 0, "cached": 0, "kept": 0, "failed": 0, "empty": 0}
    t0 = time.time()
    for i, wp_id in enumerate(ids, 1):
        st = translate_listing(wp_id)
        if st is None:
            tot["empty"] += 1
        else:
            for k in ("translated", "cached", "kept", "failed"):
                tot[k] += st[k]
        if i % 25 == 0 or i == len(ids):
            mins = (time.time() - t0) / 60
            print(f"  {i}/{len(ids)} ({mins:.1f} min) — "
                  f"tulkots {tot['translated']}, kešs {tot['cached']}, "
                  f"nemainīts {tot['kept']}, kļūdas {tot['failed']}, tukši {tot['empty']}")
        # Pauze tikai ja tiešām kaut ko darīja (kept-only iet zibenīgi)
        if st and (st["translated"] or st["cached"]):
            time.sleep(PAUSE)
    print("GATAVS:", tot)


if __name__ == "__main__":
    main()
