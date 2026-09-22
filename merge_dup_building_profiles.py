"""merge_dup_building_profiles.py — apvieno dublikātu ēku profilus.

2026-09-22 (Raimonds/Ieva, Aspazijas bulv. 20): paneļa anketas building_key
loģika atpalika no worker (#14 saīsinājumu fikss) → 'aspazijas bulv.|20|riga'
un 'aspazijas|20|riga' = DIVI profili tai pašai ēkai; anketa rādīja divus
vienādus, sludinājumi izkaisīti pa abiem.

Šis skripts: normalizē KATRAS ēkas building_key ar aktuālo worker
strip_street_type loģiku → grupē → katrā grupā par KANONISKO atstāj to ar
visvairāk listingiem (tad vecāko); pārceļ listings + scrape_inbox FK,
saplūdina auto_publish / auto_publish_phones / partnership laukus
(kanoniskajam prioritāte, tukšos aizpilda no dublikāta), dublikātu DZĒŠ.

PALAIST pēc TAM, kad deployots: worker (agent_api+migrācijas), panelis
(building-key.ts). Citādi vecā koda rakstītāji dublikātu uzreiz radīs no jauna.

    python merge_dup_building_profiles.py            # dry-run (tikai parāda)
    python merge_dup_building_profiles.py --apply    # izpilda
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv(Path(__file__).parent / ".env")

# Aktuālā normalizācija — imports no bp worker nav pieejams (cits repo), tāpēc
# minimāls spogulis: normalize + strip (identisks building_profiles_worker).
import re
import unicodedata

_LV = str.maketrans("āčēģīķļņšūž", "acegiklnsuz")
_TYPE = re.compile(r"\b(iela|iel|gatve|gat|bulvaris|bulv|prospekts|prosp|pr|"
                   r"laukums|dambis|cels|aleja|soseja|linija|krastmala|tilts|pasaza)\.?\b")
# «M.»/«L.» = «Mazā»/«Lielā» — CITA iela (zaļā 2026-09-22: M. Nometņu ≠
# Nometņu) → izvērš pilnajā vārdā, NEizmet; pārējos iniciāļus izmet.
_INI = re.compile(r"\b(?!m\.|l\.)[a-z]{1,2}\.")
_MAZA = re.compile(r"\b(?:m|maz)\.\s*")
_LIELA = re.compile(r"\b(?:l|liel)\.\s*")


def _norm(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "").strip().lower()).translate(_LV)
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if ord(c) < 128)


def normalize_key(key: str) -> str:
    """'aspazijas bulv.|20|riga' → 'aspazijas|20|riga' (aktuālā loģika).
    'm. nometnu|45|riga' → 'maza nometnu|45|riga' (NEsaplūst ar 'nometnu')."""
    parts = (key or "").split("|")
    if not parts:
        return key
    s = _norm(parts[0])
    s = _MAZA.sub("maza ", s)
    s = _LIELA.sub("liela ", s)
    s = _INI.sub(" ", s)
    s = _TYPE.sub(" ", s).replace(".", " ")
    parts[0] = re.sub(r"\s+", " ", s).strip()
    return "|".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="izpilda (bez tā dry-run)")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL trūkst")
    conn = psycopg.connect(dsn, row_factory=dict_row)

    # auto_publish_phones var vēl nebūt (migrācija 2026-08-28) — toleranti.
    has_app = conn.execute("""SELECT 1 FROM information_schema.columns
        WHERE table_schema='properties' AND table_name='building_profiles'
          AND column_name='auto_publish_phones'""").fetchone() is not None
    app_col = "b.auto_publish_phones," if has_app else "NULL AS auto_publish_phones,"
    rows = conn.execute(f"""
        SELECT b.id, b.building_key, b.full_address,
               b.auto_publish, {app_col} b.partnership_status,
               b.created_at,
               (SELECT count(*) FROM properties.listings l
                 WHERE l.building_profile_id = b.id) AS n_listings
        FROM properties.building_profiles b
    """).fetchall()

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(normalize_key(r["building_key"]), []).append(r)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"Profili: {len(rows)}; dublikātu grupas: {len(dup_groups)}")

    merged = 0
    for nkey, members in sorted(dup_groups.items()):
        # Kanoniskais: visvairāk listingu; tad vecākais (mazākais created_at).
        members.sort(key=lambda m: (-(m["n_listings"] or 0),
                                    m["created_at"] or 0, m["id"]))
        target, dups = members[0], members[1:]
        print(f"\n[{nkey}] KANONISKAIS #{target['id']} "
              f"'{target['building_key']}' ({target['n_listings']} listingi)")
        for d in dups:
            print(f"   ← saplūdina #{d['id']} '{d['building_key']}' "
                  f"({d['n_listings']} listingi)")
            if not args.apply:
                continue
            with conn.transaction():
                conn.execute("UPDATE properties.listings SET building_profile_id=%s "
                             "WHERE building_profile_id=%s", (target["id"], d["id"]))
                conn.execute("UPDATE properties.scrape_inbox SET building_profile_id=%s "
                             "WHERE building_profile_id=%s", (target["id"], d["id"]))
                # Saplūdina svarīgos manuāli kurētos laukus (tukšos aizpilda).
                app_set = ("auto_publish_phones = COALESCE(t.auto_publish_phones, "
                           "d.auto_publish_phones),") if has_app else ""
                conn.execute(f"""
                    UPDATE properties.building_profiles t SET
                      auto_publish = COALESCE(t.auto_publish, d.auto_publish),
                      {app_set}
                      partnership_status = COALESCE(t.partnership_status, d.partnership_status),
                      building_name = COALESCE(t.building_name, d.building_name),
                      primary_phone = COALESCE(t.primary_phone, d.primary_phone),
                      responsible = COALESCE(t.responsible, d.responsible)
                    FROM properties.building_profiles d
                    WHERE t.id = %s AND d.id = %s
                """, (target["id"], d["id"]))
                # matches_listings u.c. FK — ja tabula referencē bp, pārceļ.
                conn.execute("""
                    UPDATE properties.matches_listings SET building_profile_id=%s
                    WHERE building_profile_id=%s
                """, (target["id"], d["id"]))
                conn.execute("DELETE FROM properties.building_profiles WHERE id=%s",
                             (d["id"],))
            merged += 1
        # Kanoniskā atslēga → normalizētā forma (lai worker upsert trāpa).
        if args.apply and target["building_key"] != nkey:
            try:
                with conn.transaction():
                    conn.execute("UPDATE properties.building_profiles "
                                 "SET building_key=%s WHERE id=%s",
                                 (nkey, target["id"]))
            except Exception as e:
                print(f"   ! atslēgas pārrakstīšana neizdevās (paliek vecā): {e}")

    print(f"\n{'IZPILDĪTS' if args.apply else 'DRY-RUN'}: saplūdināti {merged} dublikāti.")
    conn.close()


if __name__ == "__main__":
    main()
