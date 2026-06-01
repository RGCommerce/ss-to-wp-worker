"""publish_to_wp.py — Melnā kaste galvenais CLI (Etaps 3.5).

VIENA komanda = pilns sludinājums uz rgcommerce.lv:
  teksts (slot-šablons) + features + taksonomijas + bildes + agents,
  create vai update (re-publish nedublē media), multi-units rebuild.

  python publish_to_wp.py --listing 17720            # publicē/atjaunina
  python publish_to_wp.py --listing 17720 --dry-run  # tikai parāda payload
  python publish_to_wp.py --listing 17720 --force     # pārpublicē bildes no jauna

Atkarības (visas gatavas): wp_publisher (rgc-mk/v3 token klients),
wp_templates (8 slot-šabloni), houzez_reverse_map (taksonomijas+features),
image_classify (manifests bilžu routēšanai — fasade pirmā, plans uz floor
plan sekciju).

Bilžu secība (2026-05-20, task #16): image_classify.ensure_classified
nodrošina manifestu `storage/listings/<id>/_image_manifest.json`. Tad:
- fasade bildes IET PIRMĀS galerijā (un featured_media = pirmā fasāde)
- interjers/cits — vidū dabiskā secībā
- plans — IZSLĒGTAS no galvenās galerijas, sūtām uz Houzez `fave_floor_plans`
  + DB `wp_floor_plan_attachment_ids` (Mig 024)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from wp_publisher import WPPublisher, WPPublisherError
from wp_templates import (render_body, render_excerpt, SUPPORTED_GROUPS,
                          seo_focus_keyphrase, seo_title, image_alt)
from wp_templates import _floor as _clean_floor, _trim_dec  # stāva + decimālu tīrīšana
import houzez_reverse_map as hrm
import image_pipeline  # Zilās kastes AI bilžu skaistināšana (Seedream)
import image_classify  # Bilžu klasifikators (fasade/plans/interjers/cits)
import ai_text  # AI-ģenerēts apraksts (OpenAI), fallback uz wp_templates

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(Path(__file__).parent / "storage")))

# WP Houzez agent post ID (memory project_melna_kaste_agent_assignment)
AGENT_IEVA = 4238
AGENT_RAIMONDS = 4456
IEVA_GROUPS = {"Birojs", "Medicīna", "Tirdzniecība", "Studija"}


def _agent_id(space_group: str, price_type: str) -> int:
    if str(price_type or "").strip().lower() in _SALE_PT:
        return AGENT_RAIMONDS  # visi buy/pārdod → Raimonds
    return AGENT_IEVA if space_group in IEVA_GROUPS else AGENT_RAIMONDS


def _fetch(conn, listing_id: int) -> tuple[dict, dict]:
    cur = conn.execute(
        "SELECT * FROM properties.listings WHERE id = %s", (listing_id,)
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"Listing {listing_id} nav atrasts DB.")
    listing = dict(zip([d[0] for d in cur.description], row))

    bp = {}
    if listing.get("building_profile_id"):
        bcur = conn.execute(
            "SELECT * FROM properties.building_profiles WHERE id = %s",
            (listing["building_profile_id"],),
        )
        brow = bcur.fetchone()
        if brow:
            bp = dict(zip([d[0] for d in bcur.description], brow))
    return listing, bp


def _title(listing: dict, bp: dict) -> str:
    """Title VIENMĒR = adrese (memory project_melna_kaste_template_slot_based)."""
    full = (bp.get("full_address") or "").strip()
    if full:
        return full
    parts = [listing.get("street"), listing.get("city")]
    addr = ", ".join(p.strip() for p in parts if p and str(p).strip())
    return addr or f"Komercīpašums (listing {listing['id']})"


def _image_paths(listing: dict) -> list[Path]:
    """TIKAI AI-apstrādātās (local_image_paths_processed) bildes.

    KRITISKS LIKUMS: raw ss.lv bildes NEKAD nedrīkst uz mājaslapas
    (aizliegts — ss.lv ūdenszīmes/tiesības). NEKĀDA fallback uz
    local_image_paths_raw vai local_image_paths_wp_raw. Ja nav processed
    → tukšs saraksts (publish_to_wp vispirms palaiž image_pipeline).
    """
    rel = listing.get("local_image_paths_processed")
    if not rel:
        return []
    paths = [STORAGE_ROOT / r for r in rel]
    return [p for p in paths if p.is_file()]


# Bilžu secības prioritātes (task #16): mazāks skaitlis = augstāka galerijā
_TYPE_ORDER = {"fasade": 0, "interjers": 1, "cits": 2}


def _split_by_manifest(img_paths: list[Path], manifest: dict
                       ) -> tuple[list[Path], list[Path]]:
    """Sadala AI-apstrādātās bildes pa manifesta `type`:
    - galvenā galerija: fasade (pirmā) + interjers + cits
    - plani: atsevišķi (uz Houzez floor plan sekciju)
    Stabils sort — vienāds type saglabā oriģinālo secību.
    Ja manifestā nav ieraksta → uzskatām par 'interjers'."""
    gallery, plans = [], []
    for p in img_paths:
        info = manifest.get(p.name) if isinstance(manifest, dict) else None
        t = (info or {}).get("type", "interjers")
        if t == "plans":
            plans.append(p)
        else:
            gallery.append(p)
    gallery.sort(key=lambda p: _TYPE_ORDER.get(
        (manifest.get(p.name) or {}).get("type", "interjers"), 1))
    return gallery, plans


_SALE_PT = {"regular", "parastā", "parasta", "sale", "sell", "buy"}

# Houzez Fields-builder MULTI SELECT opcijas (EXACT no Raimonda screenshot
# 2026-05-17). Vērtībai JĀSAKRĪT precīzi ar opcijas tekstu.
_M2_BUCKETS = [
    (50, "0-50 m²"), (100, "50-100 m²"), (150, "100-150 m²"),
    (200, "150-200 m²"), (300, "200-300 m²"), (500, "300-500 m²"),
    (700, "500-700 m²"), (1000, "700-1000 m²"), (1500, "1000-1500 m²"),
    (2000, "1500-2000 m²"), (2500, "2000-2500 m²"), (3000, "2500-3000 m²"),
]
_M2_TOP = "3000+ m²"


def _m2_bucket(area) -> str:
    """area_m2 skaitlis → Houzez fave_kv-m opcija (piem. 125 → '100-150 m²')."""
    try:
        a = float(str(area).replace(",", "."))
    except (TypeError, ValueError):
        return ""
    for upper, label in _M2_BUCKETS:
        if a < upper:
            return label
    return _M2_TOP


def _floor_display(floor_raw) -> str:
    """Precīzais stāvs Houzez 'Stāvs' (fave_stc481vs) opcijas formātā:
    '1'→'1.stāvs', '1.stāvs'→'1.stāvs', 'Pagrabs'/'Vienstāvu'/'Divstāvu' paliek.
    (Raimonds: VIENMĒR precīzs, nekādu bucket — feedback_floor_exact_no_buckets.)"""
    s = str(floor_raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "stāv" in low or "stav" in low:  # jau 'X.stāvs' / 'Vienstāvu' / 'Divstāvu'
        return s
    if low in ("pagrabs", "pagrabstāvs", "pagrabstavs", "p", "-1"):
        return "Pagrabs"
    m = __import__("re").match(r"^-?(\d+)\s*\.?\s*$", s)
    if m:
        n = int(m.group(1))
        return "Pagrabs" if n <= 0 else f"{n}.stāvs"
    return s


def _floor_search(floor_raw) -> str:
    """Grupētais stāvs Houzez 'Search stāvs' (fave_search-stc481vs):
    Pagrabs / 1.stāvs / 2+ (sk. crm/bulk_search_floor_wp.search_floor)."""
    if floor_raw is None:
        return ""
    s = str(floor_raw).strip()
    if s == "" or s in ("2+", "1+", "None"):
        return ""
    low = s.lower()
    if low in ("pagrabs", "pagrabstāvs", "pagrabstavs"):
        return "Pagrabs"
    if low == "vienstāvu":
        return "1.stāvs"
    _re = __import__("re")
    if _re.match(r"^(div|trīs|tris|četr|piec|sest|septiņ|astoņ|deviņ|desmit)stāv[ua]?$", low):
        return "2+"
    m = _re.match(r"^(\d+)\s*\.?\s*(stāvi?s?)?\s*$", s, _re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n == 1:
            return "1.stāvs"
        if 2 <= n <= 50:
            return "2+"
    return ""


def _numeric(v):
    """Atgriež skaitli kā str, ja vērtība ir skaitliska; citādi None.
    Houzez fave_property_rooms/bathrooms/size gaida skaitli — 'unknown'
    vai 'WC telpā' tur lauž single-property render (HTTP 500)."""
    import re as _re
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("", "unknown", "nezināms", "nav", "-"):
        return None
    m = _re.search(r"\d+[.,]?\d*", s)
    return m.group(0).replace(",", ".") if m else None


def _meta(listing: dict, bp: dict) -> dict:
    g = lambda k: (listing.get(k) if listing.get(k) not in (None, "") else None)
    price_type = str(listing.get("price_type") or "").strip().lower()
    is_sale = price_type in _SALE_PT
    price_n = _numeric(g("price"))
    area_n = _numeric(g("area_m2"))
    # m² cena = cena / platība (Houzez fave_property_sec_price, kā Ievas)
    sec_price = ""
    try:
        if price_n and area_n and float(area_n) > 0:
            sec_price = _trim_dec(round(float(price_n) / float(area_n), 2))
    except (ValueError, ZeroDivisionError):
        sec_price = ""
    land_n = _numeric(g("Zemes_gabals_m2"))
    m = {
        "fave_property_price":        _trim_dec(price_n) if price_n else "",
        "fave_property_price_postfix": "" if is_sale else "Mēnesī",
        "fave_property_sec_price":    sec_price,                 # m² cena
        "fave_property_size":         _trim_dec(area_n) if area_n else "",
        "fave_property_size_prefix":  "m²",
        # Raimonda LV Houzez setup: "Telpas" lauks = fave_property_bedrooms
        # (NE fave_property_rooms — tas Tavā setup-ā ir "Virtuves"!).
        # Telpu skaitu (Cik_telpas) liekam fave_property_bedrooms.
        "fave_property_bedrooms":     _numeric(g("Cik_telpas")) or "",
        "fave_property_bathrooms":    _numeric(g("cik_WC")) or "",
        # Zemes platība — tikai ja DB Zemes_gabals_m2 nav NULL
        "fave_property_land":         land_n or "",
        "fave_property_land_postfix": "m²" if land_n else "",
        "fave_property_address":      _title(listing, bp),
        "fave_property_map_address":  _title(listing, bp),
        "fave_property_city":         g("city") or "",
        # Houzez Fields-builder SELECT (bucket opcijas). Raimonds 2026-05-18:
        # iepriekš sūtīts kā list → serializēts masīvs → Houzez get_post_meta
        # (..,true) sagaida plain STRING → neatzīmējās. Tagad = plain string.
        "fave_kv-m":     _m2_bucket(area_n) or "",
        "fave_stc481vs":        _floor_display(g("floor")),          # precīzs "Stāvs"
        "fave_search-stc481vs": _floor_search(g("floor")),           # grupēts "Search stāvs"
        # SEO: RĀDĪT meklētājos (Raimonds 2026-05-18 — labots; iepriekš kļūdaini
        # noindex). Sūtam EXPLICIT allow, lai pārrakstītu veco noindex postmeta
        # iepriekš publicētajiem (payload noņemšana vien NEdzēš veco meta).
        # Yoast: noindex '2'=Yes(index), nofollow '0'=follow.
        "_yoast_wpseo_meta-robots-noindex":  "2",
        "_yoast_wpseo_meta-robots-nofollow": "0",
        "rgc_listing_id":             listing["id"],
        "rgc_building_profile_id":    listing.get("building_profile_id") or 0,
        "rgc_sync_source":            listing.get("source") or "ss_lv_auto",
    }
    # Skaitliskos tukšos NEsūta (Houzez intval uz '' arī OK, bet drošāk izlaist)
    out = {k: v for k, v in m.items() if v not in (None, "")}
    # "Virtuves" (fave_property_rooms) — VIENMĒR tīrām uz tukšu: auto-publish
    # nav virtuvju skaita datu, un iepriekš kļūdaini te nokļuva telpu skaits.
    # Sūtam EXPLICIT "" (citādi vecā vērtība paliek, kā Yoast noindex gadījumā).
    out["fave_property_rooms"] = ""
    return out


def publish(listing_id: int, dry_run: bool = False, force: bool = False,
            skip_ai: bool = False) -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL nav .env failā.")

    with psycopg.connect(DATABASE_URL) as conn:
        listing, bp = _fetch(conn, listing_id)

        # AI bilžu skaistināšana (Zilās kastes Seedream pipeline) — uz WP
        # IET TIKAI smukās ai_ready bildes, NE raw ss.lv. Ja vēl nav
        # processed → palaiž image_pipeline ($0.04/bilde, ~30-40s/bilde;
        # idempotents — re-publish nepārtērē, jo skip ja jau apstrādāts).
        if not listing.get("local_image_paths_processed") and not skip_ai:
            if dry_run:
                print("  [dry-run] processed bilžu nav — reālā palaišanā "
                      "vispirms ietu image_pipeline (AI skaistināšana)")
            else:
                print("  → nav AI-skaistinātu bilžu, palaižu image_pipeline "
                      "(Seedream, $0.04/bilde)...")
                from psycopg.rows import dict_row
                with psycopg.connect(DATABASE_URL,
                                     row_factory=dict_row) as ipconn:
                    res = image_pipeline.process_listing(ipconn, listing_id,
                                                         force=False)
                    ipconn.commit()
                print(f"  → image_pipeline: {res.get('status')} "
                      f"({res.get('processed_count', res.get('image_count','?'))} bildes)")
                listing, bp = _fetch(conn, listing_id)  # re-fetch ar jaunajiem

        # STINGRS DROŠĪBAS VĀRTS: NEKAD nepublicē bez AI-apstrādātām bildēm.
        # Raw ss.lv bildes uz mājaslapas = AIZLIEGTS. Ja nav processed
        # (skip_ai bez esošām, vai image_pipeline neizdevās) → ATSAKĀS.
        if not dry_run and not _image_paths(listing):
            raise SystemExit(
                f"ATCELTS: listing {listing_id} nav AI-apstrādātu bilžu "
                f"(local_image_paths_processed tukšs vai faili nav uz diska). "
                f"Raw ss.lv bildes uz WP ir AIZLIEGTAS. Palaid bez --skip-ai "
                f"(vai vispirms: python image_pipeline.py --listing {listing_id})."
            )

        sg = (listing.get("Space_group") or "").strip()
        if sg not in SUPPORTED_GROUPS:
            raise SystemExit(
                f"Space_group '{sg}' nav atbalstīts (listing {listing_id}). "
                f"Atbalstītie: {SUPPORTED_GROUPS}"
            )

        title = _title(listing, bp)
        # Teksts no building_profile lokācijai + listing pārējam
        tdata = dict(listing)
        for loc in ("city", "district", "street"):
            if bp.get(loc):
                tdata[loc] = bp[loc]
        # Teksts = template no JAU-AI-analizētajiem DB laukiem (Agent_comment,
        # Building_description u.c.). NAV live OpenAI (sk. memory).
        body = render_body(sg, listing, bp)
        excerpt = render_excerpt(sg, tdata)
        price_type = listing.get("price_type")
        agent = _agent_id(sg, price_type)
        meta = _meta(listing, bp)
        meta["fave_agents"] = str(agent)
        meta["fave_agent_display_option"] = "agent_info"
        # Yoast SEO auto-fill (v5 plugin atļauj _yoast_wpseo_ meta)
        meta["_yoast_wpseo_focuskw"] = seo_focus_keyphrase(sg, tdata)
        meta["_yoast_wpseo_title"] = seo_title(sg, tdata, title)
        meta["_yoast_wpseo_metadesc"] = excerpt  # īss konspekts ≤100 z.
        # Bilžu ALT teksts (visām bildēm vienāds — veids/rajons/m²/cena)
        alt_txt = image_alt(sg, tdata)

        existing_wp_id = listing.get("wp_post_id")
        existing_att = listing.get("wp_attachment_ids") or []
        existing_plan_att = listing.get("wp_floor_plan_attachment_ids") or []
        img_paths = _image_paths(listing)

        # Task #16: klasificē raw bildes un sadali galerijas / plānos.
        # ensure_classified ir KEŠOTS — atkārtota palaišana = $0.
        manifest = {}
        try:
            manifest = image_classify.ensure_classified(
                STORAGE_ROOT, listing_id)
        except SystemExit as e:
            print(f"  ! klasifikators izlaists ({e}) — visi gallery")
        except Exception as e:
            print(f"  ! klasifikators kļūda ({e!s:.120}) — visi gallery")
        gallery_paths, plan_paths = _split_by_manifest(img_paths, manifest)

        print(f"=== Listing {listing_id} → WP publish ===")
        print(f"  Space_group : {sg}")
        print(f"  Title       : {title}")
        print(f"  Agents      : {agent} ({'Ieva' if agent==AGENT_IEVA else 'Raimonds'})")
        print(f"  WP post     : {'UPDATE #'+str(existing_wp_id) if existing_wp_id else 'CREATE jauns'}")
        reuse_note = (f"reuse gallery={len(existing_att)} "
                      f"plans={len(existing_plan_att)}"
                      if (existing_att or existing_plan_att) and not force
                      else "augšuplādē")
        print(f"  Bildes      : galerija={len(gallery_paths)} plāni={len(plan_paths)} ({reuse_note})")
        print(f"  Body        : {len(body)} sim.")

        if dry_run:
            print("\n--- DRY-RUN payload ---")
            print("META:", meta)
            print("TAXONOMIES: (atrisina caur ensure_term reālā palaišanā)")
            print("BODY:\n", body)
            print("EXCERPT:", excerpt)
            print("\n(dry-run — nekas netika sūtīts uz WP)")
            return

        wp = WPPublisher()

        # 1. Taksonomijas + features (ensure_term auto-create)
        tax = hrm.resolve_taxonomy_terms(
            wp,
            space_group=sg,
            price_type=price_type,
            building_class=(listing.get("building_class") or None),
            city=(bp.get("city") or listing.get("city") or None),
            district=(bp.get("district") or listing.get("district") or None),
            potential=[p.strip() for p in
                       str(listing.get("Potential_space_group") or "").split(",")
                       if p.strip()],
        )
        feat_ids = hrm.resolve_feature_terms(wp, listing)
        if feat_ids:
            tax.setdefault("property_feature", []).extend(feat_ids)

        # Houzez PRASA property_status (badge/label) — bez tā single-property
        # render kraš (HTTP 500). Ja price_type nav DB → default "Nomā"
        # (RGC pārsvarā komercnoma; Raimonds koriģē per-listing vēlāk).
        if not tax.get("property_status"):
            st = wp.ensure_term("property_status", "Nomā")
            if st.get("term_id"):
                tax["property_status"] = [int(st["term_id"])]
                print("  · property_status nav datu → default 'Nomā'")

        # 2. Bildes — augšuplādē/reuse VISAS filename-order, tad split pa
        # manifesta `type`. wp_attachment_ids glabā visus IDs filename-order
        # (lai būtu deriv mapping filename→ID nākamajām re-publish).
        # wp_floor_plan_attachment_ids = derivēts plānu apakšsaraksts.
        if existing_att and len(existing_att) == len(img_paths) and not force:
            print(f"  → reuse {len(existing_att)} esošos attachment ID "
                  f"(filename-order)")
            all_attach_ids = list(existing_att)
        else:
            all_attach_ids = []
            for p in img_paths:
                if not p.is_file():
                    print(f"  ! bilde nav atrasta, izlaiž: {p}")
                    continue
                res = wp.upload_media(p, filename=p.name, alt=alt_txt)
                all_attach_ids.append(res["id"])
                print(f"  → augšuplādēts {p.name} → att {res['id']}")

        # Filename→ID mapping pēc filename-order zip
        fname_to_id = {p.name: aid for p, aid
                       in zip(img_paths, all_attach_ids)}
        gallery_ids = [fname_to_id[p.name] for p in gallery_paths
                       if p.name in fname_to_id]
        plan_attach_ids = [fname_to_id[p.name] for p in plan_paths
                           if p.name in fname_to_id]

        if gallery_ids:
            # Houzez sagaida fave_property_images = flat list ar STRING ID
            # (strādājošo property formāts: ['22664']). Int masīvs → Houzez
            # schema atmet (meta=None) + lightbox.php kraš. Tāpēc str().
            meta["fave_property_images"] = [str(i) for i in gallery_ids]
        # Floor plans — plugin (v5.1.0) servera pusē būvē Houzez Meta Box
        # grupu 'floor_plans' no šiem attachment ID-iem (fave_plan_image =
        # wp_get_attachment_url; tas ir file_input/URL lauks). Vienmēr sūtam
        # (arī tukšu sarakstu) — lai plugin var iztīrīt veco, ja plānu nav.
        print(f"  → galerija: {len(gallery_ids)} bildes "
              f"(featured = att {gallery_ids[0] if gallery_ids else 'n/a'})")
        if plan_attach_ids:
            print(f"  → plāni:    {len(plan_attach_ids)} → Houzez floor_plans")

        # 3. Create vai update — featured_media = pirmā galerijā (fasade ja ir),
        # floor_plan_attachment_ids → plugin būvē Houzez 'floor_plans' grupu
        common = dict(
            title=title, content=body, excerpt=excerpt, status="publish",
            author=agent, meta=meta, taxonomies=tax,
            featured_media=(gallery_ids[0] if gallery_ids else None),
            floor_plan_attachment_ids=plan_attach_ids,
            floor_plan_title="Telpu plāns",
        )
        if existing_wp_id:
            resp = wp.update_property(int(existing_wp_id), **common)
            wp_id = int(existing_wp_id)
        else:
            resp = wp.create_property(**common)
            wp_id = int(resp["id"])
        print(f"  ✓ WP property {wp_id}  ({resp.get('link')})")

        # 4. multi-units rebuild (pa building_profile_id)
        if listing.get("building_profile_id"):
            try:
                rb = wp.rebuild_multi_units(wp_id)
                print(f"  ✓ multi-units grupa: {rb.get('group_size')}")
            except WPPublisherError as e:
                msg = str(e)
                if "no_bpi" in msg or "400" in msg:
                    print("  · multi-units: nav grupas (OK)")
                else:
                    print(f"  ! multi-units kļūda: {msg[:120]}")

        # 5. Saglabā atpakaļ DB. wp_attachment_ids = VISI IDs filename-order
        # (lai turpmākas re-publish var derivet split no manifesta).
        # wp_floor_plan_attachment_ids = plānu apakšsaraksts (derīvēts).
        conn.execute(
            """UPDATE properties.listings
               SET wp_post_id = %s, wp_synced_at = now(),
                   wp_attachment_ids = %s,
                   wp_floor_plan_attachment_ids = %s
               WHERE id = %s""",
            (wp_id, all_attach_ids or None,
             plan_attach_ids or None, listing_id),
        )
        conn.commit()
        print(f"  ✓ DB atjaunināts: wp_post_id={wp_id}, "
              f"all_ids={len(all_attach_ids)}, "
              f"gallery={len(gallery_ids)}, plans={len(plan_attach_ids)}")
        print(f"\nGATAVS. Sludinājums: {resp.get('link')}")


def main():
    ap = argparse.ArgumentParser(description="Melnā kaste — publicē listing uz WP")
    ap.add_argument("--listing", type=int, required=True, help="listings.id")
    ap.add_argument("--dry-run", action="store_true", help="tikai parāda payload")
    ap.add_argument("--force", action="store_true",
                    help="pārpublicē bildes no jauna (ignorē esošos attachment)")
    ap.add_argument("--skip-ai", action="store_true",
                    help="NEpalaiž image_pipeline (lieto esošās bildes; $ netērē)")
    args = ap.parse_args()
    try:
        publish(args.listing, dry_run=args.dry_run, force=args.force,
                skip_ai=args.skip_ai)
    except WPPublisherError as e:
        print(f"\nWP KĻŪDA: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
