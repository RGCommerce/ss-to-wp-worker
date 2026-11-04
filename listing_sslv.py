"""«Atrast ss.lv» vienam sludinājumam (Raimonds 2026-11-04).

Sludinājums DB bez bildēm/plāna (partnera saraksts) → vēlāk ss.lv parādās tā pati telpa → aģents to atrod un
«Pievienot»: ss.lv bildes (caur ūdenszīmju tīrīšanu) + apraksts + saite → sludinājums pāranalizēts.
Kandidāti: skrāpera inbox, redzēts pēdējās 7 dienās, tā pati ēka / iela / partnera numurs, platība ±25 %.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("listing_sslv")
_status: dict[int, dict[str, Any]] = {}
_lock = threading.Lock()
LIVE_DAYS = 7


def candidates(listing_id: int) -> dict:
    import partner_intake as pi
    with psycopg.connect(pi.DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        l = conn.execute("""SELECT id, street, area_m2, floor, building_profile_id, phone_numbers FROM properties.listings
                             WHERE id=%s""", (listing_id,)).fetchone()
        if not l:
            return {"items": [], "error": "sludinājums nav atrasts"}
        area = pi._num(l.get("area_m2"))
        tails = [d[-8:] for d in ("".join(ch for ch in p if ch.isdigit()) for p in str(l.get("phone_numbers") or "").split(",")) if len(d) >= 8]
        rows = conn.execute("""SELECT id, street, area_m2, floor, price, link, phone_numbers, last_updated, building_profile_id
                                 FROM properties.scrape_inbox
                                WHERE link IS NOT NULL
                                  AND nullif(last_updated,'')::timestamptz >= now() - make_interval(days => %s)
                                  AND (building_profile_id = %s
                                       OR translate(lower(street),'āēīōūčšžģķļņŗ','aeiouczsgklnr') ~ %s
                                       OR translate(lower(street),'āēīōūčšžģķļņŗ','aeiouczsgklnr') ~ %s
                                       OR EXISTS (SELECT 1 FROM unnest(string_to_array(coalesce(phone_numbers,''), ',')) p
                                                   WHERE right(regexp_replace(p,'[^0-9]','','g'), 8) = ANY(%s)))
                                ORDER BY id DESC LIMIT 200""",
                            (LIVE_DAYS, l.get("building_profile_id") or -1,
                             pi._street_like(l.get("street") or "") or "^$nav", pi._street_like(pi._street_short(l.get("street") or "") or "") or "^$nav",
                             tails or ["~"])).fetchall()
        out = []
        for r in rows:
            a = pi._num(r.get("area_m2"))
            if area and a and abs(a - area) > max(3.0, 0.25 * area):
                continue
            taken = conn.execute("SELECT id FROM properties.listings WHERE link=%s AND id<>%s LIMIT 1", (r["link"], listing_id)).fetchone()
            out.append({"link": r["link"], "url": f"https://www.{r['link']}" if not str(r["link"]).startswith("http") else r["link"],
                        "street": r.get("street"), "area_m2": a, "floor": r.get("floor"), "price": pi._num(r.get("price")),
                        "seen": str(r.get("last_updated") or "")[:10], "taken_by": int(taken["id"]) if taken else None,
                        "diff": abs(a - area) if (a and area) else 9e9})
        out.sort(key=lambda x: x["diff"])
        for x in out:
            x.pop("diff", None)
        return {"items": out[:10]}


def attach(listing_id: int, link: str, requested_by: Optional[str] = None) -> dict:
    import ai_text_helpers as helpers
    import download_images
    import listing_reanalyze
    import partner_intake as pi
    import sslv_import
    link = pi._bare_sslv(link)
    with psycopg.connect(pi.DATABASE_URL, row_factory=dict_row) as conn:
        l = conn.execute("""SELECT id, area_m2, link, original_text, local_image_paths_raw FROM properties.listings WHERE id=%s""",
                         (listing_id,)).fetchone()
        inbox = conn.execute("SELECT street FROM properties.scrape_inbox WHERE link=%s ORDER BY id DESC LIMIT 1", (link,)).fetchone()
        if not l:
            return {"state": "error", "message": "sludinājums nav atrasts"}
        html_doc = sslv_import._fetch_html(f"https://www.{link}", helpers.HEADERS, helpers.REQUEST_TIMEOUT)
        ok, why = pi._sslv_live_matches(html_doc, (inbox or {}).get("street"), {"area_m2": None})
        if not ok or "phone_td" not in html_doc:
            return {"state": "error", "message": f"ss.lv sludinājums vairs neatbilst ({why})"}
        gallery = pi._alive_sslv_images((helpers.extract_gallery_urls(html_doc) or [])[:pi.SSLV_MAX_PHOTOS])
        raw_dir = pi.STORAGE_ROOT / "listings" / str(listing_id) / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        new_rel: list[str] = []
        for i, u in enumerate(gallery, start=1):
            data = download_images.download_one(u)
            if not data:
                continue
            fn = f"img_ss_{i:03d}.jpg"   # ne img_001 — neaiztiek esošās; ne img_user_ — iet caur ūdenszīmju tīrīšanu
            (raw_dir / fn).write_bytes(data)
            new_rel.append(f"listings/{listing_id}/raw/{fn}")
        if not new_rel:
            return {"state": "error", "message": "ss.lv bildes neielādējās"}
        old = [str(p) for p in (l.get("local_image_paths_raw") or []) if str(p) not in new_rel]
        text = (sslv_import._extract_listing_text(html_doc) or "").strip()
        conn.execute("""UPDATE properties.listings
                           SET local_image_paths_raw = %s, images_downloaded_at = now(),
                               original_text = CASE WHEN coalesce(original_text,'') = '' THEN %s ELSE original_text END,
                               link = CASE WHEN coalesce(link,'') = '' AND NOT EXISTS (SELECT 1 FROM properties.listings x WHERE x.link=%s AND x.id<>%s)
                                           THEN %s ELSE link END,
                               "Debug_note" = left(coalesce("Debug_note",'') || %s, 1000), updated_at = now()
                         WHERE id = %s""",
                     (new_rel + old, text or None, link, listing_id, link,
                      f" | ss.lv pievienots {datetime.now().date()} ({requested_by or 'panelis'}): {link}", listing_id))
        conn.execute("UPDATE properties.scrape_inbox SET skip_reason = coalesce(skip_reason, %s) WHERE link = %s",
                     (f"transferred: listing #{listing_id}", link))
        conn.commit()
    img = pi._process_images(listing_id)
    res = listing_reanalyze.run(listing_id, requested_by)
    return {"state": res.get("state"), "message": res.get("message"), "photos": len(new_rel),
            "processed": img.get("processed"), "republished": res.get("republished")}


def start_attach(listing_id: int, link: str, requested_by: Optional[str] = None) -> dict:
    with _lock:
        cur = _status.get(listing_id)
        if cur and cur.get("state") == "running":
            return cur
        _status[listing_id] = {"listing_id": listing_id, "state": "running"}

    def _job():
        try:
            res = attach(listing_id, link, requested_by)
        except Exception as e:
            logger.exception("ss.lv pievienot #%s", listing_id)
            res = {"state": "error", "message": f"{type(e).__name__}: {str(e)[:200]}"}
        with _lock:
            _status[listing_id] = {"listing_id": listing_id, **res, "finished_at": datetime.now(timezone.utc).isoformat()}

    threading.Thread(target=_job, name=f"sslv-attach-{listing_id}", daemon=True).start()
    return _status[listing_id]


def status(listing_id: int) -> dict:
    return _status.get(listing_id) or {"listing_id": listing_id, "state": "idle"}
