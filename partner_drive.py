"""Sadarbības partneru Google Drive mapju AUTOMĀTISKA skenēšana (Raimonds 2026-10-25).

Skanstes City (Mārtiņš Škapars) savus brīvos birojus tur publiskā Drive mapē: katra telpa = apakšmape
«<platība> m² (<stāvs>), <adrese>[ - piezīme]» ar foto + plānu; cenas atsevišķā PDF (tabula: platība,
stāvs, tips, ēkas kods, statuss, noma, fit-out, apsaimniekošana; FLEX tabulas). Tādu partneru var būt vairāki.

Princips: skenējam KATRU DIENU (mapes saraksts + cenu fails — bez AI, nemaksā), un tikai tad, ja kaut kas
MAINĪJIES (jauna/dzēsta mape, cits cenu fails), uzbūvējam ieplūdi tieši tādu pašu kā e-pastam
(partner_intake: analyze → reconcile → Telegram → panelī «Partneru piedāvājumi» → apstiprina Raimonds/Ieva).
Ja nekas nav mainījies — nekas nenotiek. Ja iepriekšējais šī avota piedāvājums vēl nav apstiprināts —
jaunu neveido (lai nekrājas), nākamajā dienā mēģina vēlreiz.

Avoti glabājas volume failā partner_intake/_drive_sources.json (bez DB migrācijas); panelī var pievienot/
dzēst/«Skenēt tagad».
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("partner_drive")

SCHEDULES = {"hourly": timedelta(hours=1), "daily": timedelta(hours=24), "weekly": timedelta(days=7)}
SSLV_DEFAULT_DAYS = 30        # ss.lv ievietošanas datums pēdējās N dienās (Barona aeonb/hpmnn bija 16/23 d. veci, 2026-10-28)
SSLV_LIVE_DAYS = 7            # scrape_inbox rinda = «dzīva», ja skrāperis to redzējis pēdējās N dienās
SSLV_URL_TYPES = (("/offices/", "Birojs"), ("/premises/shops/", "Tirdzniecība"), ("/premises/storehouses", "Noliktava"),
                  ("/storehouses", "Noliktava"), ("/premises/production", "Ražošana"), ("/premises/catering", "Restorans/Cafe"),
                  ("/premises/beauty", "Studija"), ("/premises/medical", "Medicīna"), ("/plots-and-lands/", "Zeme"))
SSLV_MAX_UNITS = 30           # drošības griesti vienai ieplūdei
MAX_FOLDER_DEPTH = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sources_path() -> Path:
    import partner_intake
    partner_intake.INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    return partner_intake.INTAKE_DIR / "_drive_sources.json"


def load_sources() -> list[dict]:
    p = _sources_path()
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("partner_drive: _drive_sources.json nenolasās")
        return []


def save_sources(sources: list[dict]) -> None:
    _sources_path().write_text(json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8")


def _drive_id(value: str, kind: str) -> Optional[str]:
    """Saite vai tīrs ID → ID. kind = folder|file."""
    import link_media
    v = (value or "").strip()
    if not v:
        return None
    c = link_media.classify_url(v)
    if c and c[0] == f"gdrive_{kind}":
        return c[1]
    if re.fullmatch(r"[A-Za-z0-9_-]{15,}", v):
        return v
    return None


def upsert_source(data: dict) -> dict:
    """Panelis: {id?, kind: drive|web, name, folder | url, price_files: [saite...], agent_email, schedule, enabled}."""
    kind = (data.get("kind") or ("web" if data.get("url") else "drive")).strip().lower()
    web_url = (data.get("url") or "").strip()
    folder_id = None
    sslv: dict = {}
    if kind == "web":
        if not re.match(r"^https?://[^\s]+$", web_url):
            raise ValueError("Mājaslapas saite nav derīga (jāsākas ar http)")
    elif kind == "sslv":
        # partnera ss.lv numurs + adrese (Raimonds 2026-10-28): katrs jauns ss.lv sludinājums ar ŠO numuru ŠAJĀ ēkā
        # pēdējās N dienās → ieplūde apstiprināšanai (manuāli, nekad automātiski)
        tails = _phone_tails(data.get("phones") or data.get("contact_phone") or "")
        if not tails:
            raise ValueError("Vajag partnera telefona numuru (vismaz 7 cipari)")
        # adreses: `addresses` (saraksts «iela nr[, pilsēta]» vai {street, city}) un/vai vecais street+city
        raw_addrs: list = list(data.get("addresses") or [])
        if data.get("street"):
            raw_addrs.insert(0, {"street": data.get("street"), "city": data.get("city") or "Rīga"})
        addresses: list[dict] = []
        seen_keys: set[str] = set()
        for ra in raw_addrs:
            if isinstance(ra, str):
                parts = [x.strip() for x in ra.split(",")]
                street, city = parts[0], (parts[1] if len(parts) > 1 and parts[1] else "Rīga")
            else:
                street, city = (ra.get("street") or "").strip(), (ra.get("city") or "Rīga").strip() or "Rīga"
            if not street or not any(ch.isdigit() for ch in street):
                raise ValueError(f"Adresei vajag mājas numuru: «{street}»")
            bps = _resolve_bps(street, city)
            if not bps:
                raise ValueError(f"Ēka «{street}, {city}» DB nav atrasta — pārbaudi adresi")
            import partner_intake
            k = partner_intake._street_key(bps[0].get("full_address") or street)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            addresses.append({"street": street, "city": city, "bp_ids": [int(b["id"]) for b in bps],
                              "bp_address": bps[0].get("full_address") or street})
        if not addresses:
            raise ValueError("Vajag vismaz vienu ēkas adresi")
        try:
            days = int(data.get("days") or SSLV_DEFAULT_DAYS)
        except Exception:
            days = SSLV_DEFAULT_DAYS
        first = addresses[0]
        sslv = {"phones": sorted(tails), "addresses": addresses, "days": max(1, min(days, 120)),
                "street": first["street"], "city": first["city"], "bp_ids": first["bp_ids"], "bp_address": first["bp_address"]}
    else:
        folder_id = _drive_id(data.get("folder") or data.get("folder_id") or "", "folder")
        if not folder_id:
            raise ValueError("Drive mapes saite nav atpazīta")
    price_ids = []
    for x in data.get("price_files") or []:
        fid = _drive_id(x, "file")
        if fid:
            price_ids.append(fid)
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Partnera nosaukums ir obligāts")
    sources = load_sources()
    sid = (data.get("id") or "").strip() or uuid.uuid4().hex[:8]
    cur = next((s for s in sources if s.get("id") == sid), None)
    src = {
        **(cur or {}),
        "id": sid, "name": name, "kind": kind, "folder_id": folder_id, "url": web_url or None, "price_file_ids": price_ids,
        "agent_email": (data.get("agent_email") or (cur or {}).get("agent_email") or "raimonds@rgcommerce.lv").strip().lower(),
        "contact_name": (data.get("contact_name") or (cur or {}).get("contact_name") or "").strip(),
        "contact_email": (data.get("contact_email") or (cur or {}).get("contact_email") or "").strip(),
        "contact_phone": (data.get("contact_phone") or (cur or {}).get("contact_phone") or "").strip(),
        "notes": (data.get("notes") or (cur or {}).get("notes") or "").strip(),
        **sslv,
        "schedule": data.get("schedule") if data.get("schedule") in SCHEDULES else (cur or {}).get("schedule") or ("hourly" if kind == "sslv" else "daily"),
        "enabled": bool(data.get("enabled", (cur or {}).get("enabled", True))),
        "created_at": (cur or {}).get("created_at") or _now().isoformat(),
    }
    if cur is None:
        sources.append(src)
    else:
        sources[sources.index(cur)] = src
    save_sources(sources)
    if kind == "sslv":
        try:
            src["trusted_synced"] = _sync_trusted_numbers(src)
        except Exception as e:
            logger.warning("partner_drive: trusted_numbers sync %s: %s", src.get("name"), e)
    return src


def _sslv_street_variants(street: str) -> set[str]:
    """«Skanstes iela 27» → {«Skanstes iela 27», «Skanstes 27»} — ss.lv/skrāpera inbox raksta bez «iela»
    (gatve/bulvāris/dambis paliek)."""
    st = re.sub(r"\s+", " ", (street or "").strip())
    out = {st} if st else set()
    short = re.sub(r"\s+iela\b", "", st, flags=re.I).strip()
    if short:
        out.add(short)
    return out


def _sync_trusted_numbers(src: dict) -> int:
    """ss.lv rāda numuru maskētu («27-07-***», atver tikai ar JS); skrāperis (sslv-scraper railway_worker) numuru
    atklāj ar Puppeteer TIKAI aģentu sludinājumiem uzticamās adresēs (properties.trusted_numbers: iela + numurs,
    prefiksa sakritība). Bez tā inbox rindai phone_numbers=NULL un mūsu avots to neatrod (Skanstes 27 bghcj,
    Raimonds 2026-10-28). Tāpēc katrs ss.lv avots pats reģistrē savas ēkas × numurus kā uzticamus un atstata
    trusted_checked_at, lai skrāperis pārbauda arī JAU esošos sludinājumus."""
    import partner_intake
    import psycopg
    from psycopg.rows import dict_row
    phones = [p for p in (src.get("phones") or []) if p]
    streets: set[str] = set()
    for a in _src_addresses(src):
        for cand in (a.get("bp_address"), a.get("street")):
            if cand:
                streets |= _sslv_street_variants(cand)
    if not phones or not streets:
        return 0
    norm = lambda x: re.sub(r"\s+", " ", (x or "").strip().lower()).translate(str.maketrans("āēīōūčšžģķļņŗ", "aeioucszgklnr"))
    added = 0
    with psycopg.connect(partner_intake.DATABASE_URL, row_factory=dict_row) as conn:
        have = {(norm(r["street"]), re.sub(r"\D", "", r["phone"] or "")[-8:])
                for r in conn.execute("SELECT street, phone FROM properties.trusted_numbers").fetchall()}
        for ph in phones:
            for st in streets:
                if (norm(st), ph[-8:]) in have:
                    continue
                conn.execute("""INSERT INTO properties.trusted_numbers (phone, street, note, created_by)
                                VALUES (%s, %s, %s, %s)""",
                             (ph, st, f"partneru avots {src.get('name')} ({src.get('id')})", "partner_intake"))
                have.add((norm(st), ph[-8:]))
                added += 1
        # jau noskrāpētie šo ēku aģentu sludinājumi bez numura → skrāperis tos pārbauda vēlreiz
        conn.execute("""UPDATE properties.scrape_inbox SET trusted_checked_at = NULL
                         WHERE lower(btrim(coalesce(is_agent,''))) = 'true'
                           AND coalesce(btrim(phone_numbers),'') = ''
                           AND translate(lower(btrim(coalesce(street,''))), 'āēīōūčšžģķļņŗ', 'aeioucszgklnr') = ANY(%s)""",
                     ([norm(x) for x in streets],))
        conn.commit()
    return added


def delete_source(sid: str) -> bool:
    sources = load_sources()
    new = [s for s in sources if s.get("id") != sid]
    if len(new) == len(sources):
        return False
    save_sources(new)
    return True


# ───────────────────────────── skenēšana ─────────────────────────────

def _fingerprint(entries: list[dict], price_blobs: dict[str, bytes]) -> str:
    h = hashlib.sha1()
    for e in sorted(entries, key=lambda x: (x.get("path") or "", x.get("id") or "")):
        h.update(f"{e.get('kind')}|{e.get('id')}|{e.get('path')}\n".encode("utf-8"))
    for fid in sorted(price_blobs):
        h.update(f"price|{fid}|{hashlib.sha1(price_blobs[fid]).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


def _unit_folders(entries: list[dict]) -> list[dict]:
    """Mapes, kas izskatās pēc telpām: nosaukumā platība (m²) VAI dziļākā «Nr.N» mape (flex). Vecākmapes,
    kurām ir apakšmapes ar platību, izlaiž (lai «FLEX Biroji» nenonāk kā telpa)."""
    folders = [e for e in entries if e.get("kind") == "folder"]
    paths = {f["path"] for f in folders}
    out = []
    for f in folders:
        name = f["path"].split("/")[-1]
        has_children = any(p.startswith(f["path"] + "/") for p in paths)
        is_unit = bool(re.search(r"\d\s*(?:m2|m²|kv\.?m)", name, re.I)) or bool(re.search(r"\bnr\.?\s*\d", name, re.I))
        if is_unit and not has_children:
            out.append(f)
    return out


def _body_text(src: dict, entries: list[dict], scanned_at: datetime) -> str:
    units = _unit_folders(entries)
    files_by_folder: dict[str, int] = {}
    for e in entries:
        if e.get("kind") != "folder" and "/" in (e.get("path") or ""):
            files_by_folder[e["path"].rsplit("/", 1)[0]] = files_by_folder.get(e["path"].rsplit("/", 1)[0], 0) + 1
    lines = [
        f"AUTOMĀTISKS SKENĒJUMS — partnera {src['name']} publiskā Google Drive mape ({scanned_at.strftime('%Y-%m-%d')}).",
        f"Partneris: {src['name']}" + (f"; kontakts: {src.get('contact_name')}" if src.get("contact_name") else "")
        + (f" {src.get('contact_email')}" if src.get("contact_email") else "")
        + (f" {src.get('contact_phone')}" if src.get("contact_phone") else ""),
        "Cenas, statuss (Pieejams / Q3 2026 = pieejams no …) un apsaimniekošana ir pielikumā (cenu fails). "
        "Katra TELPU MAPE zemāk = viena telpa: mapes nosaukumā ir platība, stāvs, adrese un piezīme "
        "(piem. «VAR DALĪT», «REMONTS», «ĒDINĀŠANAI» → partner_notes); mapē ir šīs telpas foto un plāns. "
        "Cenu failā ēkas rakstītas ar kodiem (piem. S54A = Skanstes 54A, B39 = Brīvības 39, D3 = Duntes 3) — "
        "atšifrē pēc mapju adresēm. Sasaisti katru telpu ar tās mapi laukā link_ids (platība + stāvs + adrese).",
        "",
        "TELPU MAPES (bildes):",
    ]
    if src.get("notes"):
        lines.insert(3, f"Piezīmes par partneri: {src['notes']}")
    for f in units:
        n = files_by_folder.get(f["path"], 0)
        lines.append(f"- {f['path'].replace('/', ' / ')} ({n} faili): https://drive.google.com/drive/folders/{f['id']}")
    other = [e for e in entries if e.get("kind") != "folder" and "/" not in (e.get("path") or "")]
    if other:
        lines.append("")
        lines.append("Faili mapes saknē: " + ", ".join(e["path"] for e in other[:30]))
    return "\n".join(lines)


def scan_web_source(src: dict, force: bool = False) -> dict:
    """Mājaslapas avots: saraksta lapa (nospiedums) → ja mainījies → detaļu lapas + bildes → ieplūde."""
    import partner_intake
    import partner_web
    scanned_at = _now()
    col = partner_web.collect(src["url"], recipe=src.get("web_recipe"))
    learned = None
    if col.get("needs_recipe") or (src.get("web_recipe") and not col["details"] and not col.get("list_only")):
        # nezināma lapas uzbūve (vai vecā recepte vairs nestrādā) → AI iemācās recepti VIENREIZ un to glabā pie avota
        learned = partner_web.learn_recipe(src["url"], col.get("_raw") or partner_web._get(src["url"]))
        logger.info("partner_drive %s: AI recepte %s", src["name"], {k: learned.get(k) for k in ("mode", "detail_url_regex", "block_start_regex", "notes")})
        col = partner_web.collect(src["url"], recipe=learned)
        if not col["details"] and not col.get("list_only"):
            raise RuntimeError(f"Lapu neizdodas nolasīt arī ar AI recepti ({learned.get('mode')}: {learned.get('notes')})")
    col.pop("_raw", None)
    fp = col["fingerprint"]
    result: dict[str, Any] = {"source_id": src["id"], "scanned_at": scanned_at.isoformat(), "unit_folders": len(col["details"]),
                              "price_files": 0, "changed": fp != src.get("last_fingerprint"), "fingerprint": fp,
                              "intake_id": None, "skipped": None, "web_recipe": learned, "list_only": bool(col.get("list_only"))}
    if not result["changed"] and not force:
        return result
    if not col["details"] and not col.get("list_only"):
        raise RuntimeError("Lapā neatradu nevienu telpas lapu (saites ar platību) — vajag citu URL vai pielāgojumu")
    # iepriekšējais vēl gaida apstiprināšanu, bet avots mainījies → jaunais to AIZSTĀJ (vecais = «superseded», panelī
    # pazūd; Realto 2026-10-27: pēc labojuma «Skenēt no jauna» neko nedeva, jo vecais nolasījums stāvēja ceļā)
    supersede = _pending_prev(src)
    parsed = {
        "message_id": f"web:{src['id']}:{fp[:12]}",
        "forwarder_name": src.get("name"), "forwarder_email": src.get("agent_email") or "raimonds@rgcommerce.lv",
        "subject": f"{src['name']} — mājaslapas skenējums {scanned_at.strftime('%Y-%m-%d')}",
        "body": partner_web.body_text(src, col, scanned_at),
        "orig_sender": {"name": src.get("contact_name") or src.get("name"), "email": src.get("contact_email") or ""},
        "attachments_raw": [], "date": scanned_at.isoformat(),
    }
    meta = partner_intake._store_intake(parsed, "web")
    meta["drive_source_id"] = src["id"]
    meta["drive_fingerprint"] = fp
    d = partner_intake.INTAKE_DIR / meta["intake_id"]
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "web_links.json").write_text(json.dumps(partner_web.web_links(col), ensure_ascii=False, indent=1), encoding="utf-8")
    result["intake_id"] = meta["intake_id"]
    rep = partner_intake.process_intake(meta["intake_id"], do_notify=False)
    result["summary"] = rep.get("summary")
    _finish_scan_intake(meta["intake_id"], rep, supersede, src, result)
    return result


def _phone_tails(s) -> set[str]:
    """Telefona teksts (vairāki ar , ; / |) → pēdējo 8 ciparu kopa (kā auto_publish_poller._tails)."""
    out: set[str] = set()
    for part in re.split(r"[,;/|\n]", str(s or "")):
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 7:
            out.add(digits[-8:])
    return out


def _resolve_bps(street: str, city: str) -> list[dict]:
    import partner_intake
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(partner_intake.DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        return partner_intake._find_bps(conn, street, city, None, None) or []


def _decided_sslv_ids(source_id: str) -> set[str]:
    """ss.lv sludinājumi, par kuriem Raimonds jau IR lēmis (bija apstiprinātā/daļējā šī avota ieplūdē) — nerāda otrreiz."""
    import partner_intake
    done: set[int] = set()
    if not partner_intake.INTAKE_DIR.is_dir():
        return done
    for d in partner_intake.INTAKE_DIR.iterdir():
        mp, rp = d / "meta.json", d / "report.json"
        if not mp.is_file() or not rp.is_file():
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            if meta.get("drive_source_id") != source_id or meta.get("status") not in ("applied", "partial"):
                continue
            rep = json.loads(rp.read_text(encoding="utf-8"))
            for a in rep.get("addresses") or []:
                for u in a.get("units") or []:
                    if u.get("sslv_link"):
                        done.add(_norm_link(u["sslv_link"]))
        except Exception:
            continue
    return done


def _norm_link(link) -> str:
    l = re.sub(r"^https?://", "", str(link or "").strip()).replace("www.", "", 1)
    return re.sub(r"^ss\.com/", "ss.lv/", l).split("#")[0].split("?")[0]


def _sslv_rows(src: dict) -> list[dict]:
    """ss.lv sludinājumi DB: šī avota ēka(s) + partnera numurs + pēdējās N dienas + aktīvi (Debug_status ok)."""
    import partner_intake
    import psycopg
    from psycopg.rows import dict_row
    tails = list(src.get("phones") or [])
    addrs = _src_addresses(src)
    bp_ids = sorted({int(b) for a in addrs for b in (a.get("bp_ids") or [])})
    if not tails or not bp_ids:
        return []
    pats = [f"%{t}" for t in tails]
    days = int(src.get("days") or SSLV_DEFAULT_DAYS)
    keys = {partner_intake._street_key(x) for a in addrs for x in (a.get("street"), a.get("bp_address")) if x}
    out: list[dict] = []
    seen_links: set[str] = set()
    with psycopg.connect(partner_intake.DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        # 1) jau AI-apstrādāti ss.lv listingi (properties.listings, source sslv) — apstiprinot PĀRŅEM
        rows = conn.execute(
            """SELECT id, street, city, district, area_m2, floor, price, price_type, price_per_m2, "Space_group" AS space_group,
                      "Agent_comment" AS agent_comment, link, phone_numbers, date_posted, created_at, on_website,
                      coalesce(array_length(local_image_paths_raw,1),0) AS n_raw
                 FROM properties.listings
                WHERE source = 'sslv' AND "Debug_status" = 'ok'
                  AND building_profile_id = ANY(%s)
                  AND coalesce(nullif(date_posted,'')::timestamptz, created_at) >= now() - make_interval(days => %s)
                  AND EXISTS (SELECT 1 FROM unnest(string_to_array(coalesce(phone_numbers,''), ',')) p
                               WHERE regexp_replace(p, '[^0-9]', '', 'g') LIKE ANY(%s))
                ORDER BY created_at DESC, id DESC
                LIMIT %s""",
            (bp_ids, days, pats, SSLV_MAX_UNITS)).fetchall()
        for r in rows:
            d = dict(r)
            d["origin"] = "listing"
            d["sslv_listing_id"] = int(d["id"])
            if d.get("link"):
                seen_links.add(_norm_link(d["link"]))
            out.append(d)
        # 2) skrāpera inbox (properties.scrape_inbox) — viss, kas ŠOBRĪD ir ss.lv ar šo numuru (arī bez AI apstrādes,
        #    arī ja listings rinda dzēsta): Barona Cēsu 31 aeonb/hpmnn bija tikai inbox (2026-10-28). Apstiprinot IZVEIDO.
        inbox = conn.execute(
            """SELECT id AS inbox_id, street, city, district, area_m2, floor, price, price_type, price_per_m2,
                      "Space_group" AS space_group, "Agent_comment" AS agent_comment, link, phone_numbers, date_posted,
                      last_updated, created_at, building_profile_id, uuid
                 FROM properties.scrape_inbox
                WHERE coalesce(skip_reason,'') NOT IN ('blacklisted_phone')
                  AND (building_profile_id = ANY(%s) OR building_profile_id IS NULL)
                  AND coalesce(nullif(last_updated,'')::timestamptz, created_at) >= now() - make_interval(days => %s)
                  AND coalesce(nullif(date_posted,'')::timestamptz, created_at) >= now() - make_interval(days => %s)
                  AND EXISTS (SELECT 1 FROM unnest(string_to_array(coalesce(phone_numbers,''), ',')) p
                               WHERE regexp_replace(p, '[^0-9]', '', 'g') LIKE ANY(%s))
                ORDER BY last_updated DESC NULLS LAST, id DESC
                LIMIT %s""",
            (bp_ids, SSLV_LIVE_DAYS, days, pats, SSLV_MAX_UNITS * 4)).fetchall()
        for r in inbox:
            d = dict(r)
            if not d.get("link") or _norm_link(d["link"]) in seen_links:
                continue
            if d.get("building_profile_id") not in bp_ids and partner_intake._street_key(d.get("street") or "") not in keys:
                continue   # cita ēka (bez ēkas profila → pēc ielas atslēgas)
            seen_links.add(_norm_link(d["link"]))
            d["origin"] = "inbox"
            d["id"] = int(d["inbox_id"])
            d["n_raw"] = 0
            out.append(d)
    return out[:SSLV_MAX_UNITS]


def _src_addresses(src: dict) -> list[dict]:
    """Avota adreses (jaunais `addresses` saraksts; vecajiem avotiem — street/bp_ids)."""
    if src.get("addresses"):
        return list(src["addresses"])
    if src.get("street"):
        return [{"street": src.get("street"), "city": src.get("city") or "Rīga", "bp_ids": src.get("bp_ids") or [],
                 "bp_address": src.get("bp_address") or src.get("street")}]
    return []


def _row_address(src: dict, r: dict) -> dict:
    """Kurai avota adresei pieder sludinājums (pēc ēkas profila, tad ielas atslēgas); citādi — paša sludinājuma iela."""
    import partner_intake
    bp = r.get("building_profile_id")
    for a in _src_addresses(src):
        if bp and int(bp) in [int(x) for x in (a.get("bp_ids") or [])]:
            return a
    rk = partner_intake._street_key(r.get("street") or "")
    for a in _src_addresses(src):
        if rk and rk in {partner_intake._street_key(x) for x in (a.get("street"), a.get("bp_address")) if x}:
            return a
    return {"street": r.get("street"), "city": r.get("city") or "Rīga", "bp_address": r.get("street")}


def sslv_number_addresses(phones: str, days: int = 90) -> list[dict]:
    """Kur ŠIS numurs liek ss.lv sludinājumus (skrāpera inbox + listings, pēdējās N d.): adrese, ēkas profils, skaits,
    pēdējais datums. Raimonds 2026-10-28: «rīks, kas parāda, kādas adreses šis nr vēl liek — ja interesē, pievienoju»."""
    import partner_intake
    import psycopg
    from psycopg.rows import dict_row
    tails = _phone_tails(phones)
    if not tails:
        return []
    pats = [f"%{t}" for t in tails]
    groups: dict[str, dict] = {}
    with psycopg.connect(partner_intake.DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        q = """SELECT street, city, building_profile_id AS bp, count(*) AS n,
                      max(coalesce(nullif(date_posted,'')::timestamptz, created_at)) AS last_posted,
                      bool_or(coalesce(nullif(last_updated,'')::timestamptz, created_at) >= now() - interval '7 days') AS live
                 FROM properties.scrape_inbox
                WHERE coalesce(skip_reason,'') <> 'blacklisted_phone'
                  AND coalesce(nullif(last_updated,'')::timestamptz, created_at) >= now() - make_interval(days => %s)
                  AND EXISTS (SELECT 1 FROM unnest(string_to_array(coalesce(phone_numbers,''), ',')) p
                               WHERE regexp_replace(p, '[^0-9]', '', 'g') LIKE ANY(%s))
                GROUP BY 1, 2, 3
               UNION ALL
               SELECT street, city, building_profile_id, count(*),
                      max(coalesce(nullif(date_posted,'')::timestamptz, created_at)), true
                 FROM properties.listings
                WHERE source = 'sslv' AND "Debug_status" = 'ok'
                  AND created_at >= now() - make_interval(days => %s)
                  AND EXISTS (SELECT 1 FROM unnest(string_to_array(coalesce(phone_numbers,''), ',')) p
                               WHERE regexp_replace(p, '[^0-9]', '', 'g') LIKE ANY(%s))
                GROUP BY 1, 2, 3"""
        rows = conn.execute(q, (days, pats, days, pats)).fetchall()
        for r in rows:
            street = (r.get("street") or "").strip()
            if not street or not any(ch.isdigit() for ch in street):
                continue
            k = partner_intake._street_key(street) + "|" + (r.get("city") or "").strip().lower()[:6]
            g = groups.setdefault(k, {"street": street, "city": (r.get("city") or "Rīga").strip(), "bp_id": None,
                                      "bp_address": None, "n": 0, "last_posted": None, "live": False})
            g["n"] += int(r["n"] or 0)
            g["live"] = g["live"] or bool(r.get("live"))
            if r.get("bp") and not g["bp_id"]:
                g["bp_id"] = int(r["bp"])
            lp = r.get("last_posted")
            if lp and (g["last_posted"] is None or lp > g["last_posted"]):
                g["last_posted"] = lp
        for g in groups.values():
            if g["bp_id"]:
                row = conn.execute("SELECT full_address FROM properties.building_profiles WHERE id=%s", (g["bp_id"],)).fetchone()
                g["bp_address"] = (row or {}).get("full_address")
            else:
                bps = partner_intake._find_bps(conn, g["street"], g["city"], None, None) or []
                if bps:
                    g["bp_id"] = int(bps[0]["id"])
                    g["bp_address"] = bps[0].get("full_address")
    out = sorted(groups.values(), key=lambda g: (not g["live"], -(g["n"])))
    for g in out:
        g["last_posted"] = str(g["last_posted"])[:10] if g["last_posted"] else None
    return out


def _other_addresses(src: dict) -> list[dict]:
    """Adreses, kur numurs liek sludinājumus, bet kuras avotā NAV (jauna ēka?) — panelī «+ pievienot»."""
    import partner_intake
    have_bps = {int(b) for a in _src_addresses(src) for b in (a.get("bp_ids") or [])}
    have_keys = {partner_intake._street_key(x) for a in _src_addresses(src) for x in (a.get("street"), a.get("bp_address")) if x}
    out = []
    try:
        for g in sslv_number_addresses(", ".join(src.get("phones") or []), days=90):
            if (g.get("bp_id") and g["bp_id"] in have_bps) or partner_intake._street_key(g["street"]) in have_keys:
                continue
            if g.get("live"):
                out.append(g)
    except Exception as e:
        logger.warning("partner_drive: other_addresses %s: %s", src.get("name"), e)
    return out[:10]


def _sslv_analysis(src: dict, rows: list[dict]) -> dict:
    """Gatava analīze BEZ AI: katrs ss.lv sludinājums = viena telpa (lauki no DB). `sslv_listing_id` = pārņemšanai."""
    import partner_intake
    props = partner_intake._schema()["properties"]["addresses"]["items"]["properties"]["units"]["items"]["properties"]

    def blank() -> dict:
        u: dict = {}
        for k, spec in props.items():
            t = spec.get("type")
            types = t if isinstance(t, list) else [t]
            if "array" in types:
                u[k] = []
            elif "null" in types:
                u[k] = None
            elif "boolean" in types:
                u[k] = False
            elif "string" in types:
                u[k] = ""
            else:
                u[k] = None
        return u

    units = []
    for r in rows:
        u = blank()
        sale = (r.get("price_type") or "monthly") != "monthly"
        price = partner_intake._num(r.get("price"))
        area = partner_intake._num(r.get("area_m2"))
        u.update({
            "ref": f"ss.lv #{r['id']}", "area_m2": area, "floor": str(r.get("floor") or ""),
            "deal": "sale" if sale else "rent",
            "rent_total": None if sale else price, "sale_price": price if sale else None,
            "rent_per_m2_min": (partner_intake._num(r.get("price_per_m2")) if not sale else None),
            "space_type": (r.get("space_group") if r.get("space_group") in partner_intake.SPACE_GROUPS
                           else next((t for seg, t in SSLV_URL_TYPES if seg in str(r.get("link") or "")), None)),
            "description_lv": (r.get("agent_comment") or "")[:600],
            "offer_type": "standard", "confidence": 1.0,
            "evidence": f"ss.lv sludinājums {r.get('link') or ''} ({str(r.get('date_posted') or r.get('created_at'))[:10]})",
            "partner_notes": f"ss.lv sludinājums ievietots {str(r.get('date_posted') or r.get('created_at'))[:10]}",
            "sslv_listing_id": int(r["sslv_listing_id"]) if r.get("sslv_listing_id") else None,
            "sslv_link": r.get("link"),
        })
        if r.get("origin") == "inbox":
            u["ref"] = f"ss.lv inbox #{r['id']}"
            u["evidence"] = f"ss.lv sludinājums (skrāpera inbox) {r.get('link') or ''}"
        if u.get("space_type") is None:
            u["space_type"] = None
        units.append(u)
    # telpas grupē pa avota adresēm (viens partneris — vairākas ēkas, Raimonds 2026-10-28)
    by_addr: dict[str, dict] = {}
    for r, u in zip(rows, units):
        a = _row_address(src, r)
        key = (a.get("bp_address") or a.get("street") or "").lower()
        g = by_addr.setdefault(key, {"street": a.get("bp_address") or a.get("street") or r.get("street"),
                                     "city": a.get("city") or r.get("city") or "Rīga", "district": r.get("district"),
                                     "building_name": None, "address_known": True, "list_complete_here": False, "units": []})
        g["units"].append(u)
    return {
        "is_vacancy_offer": True,
        "partner": {"company": src.get("name"), "contact_name": src.get("contact_name") or None,
                    "email": src.get("contact_email") or None, "phone": ", ".join(f"+371{t}" for t in src.get("phones") or []),
                    "website": None},
        "list_date": None, "looks_complete": False, "language": "lv",
        "addresses": list(by_addr.values()),
        "skipped_items": [], "warnings": [],
    }


def scan_sslv_source(src: dict, force: bool = False) -> dict:
    """Partnera ss.lv numurs + ēka: jauni sludinājumi pēdējās N dienās → ieplūde (bez AI). Nospiedums = sludinājumu id kopa
    (mīnus jau izlemtie) → mainās tikai, kad parādās jauns. Vecā neapstiprinātā ieplūde tiek aizstāta."""
    import partner_intake
    scanned_at = _now()
    rows = _sslv_rows(src)
    decided = _decided_sslv_ids(src["id"])
    rows = [r for r in rows if _norm_link(r.get("link")) not in decided]
    ids = sorted(_norm_link(r.get("link")) or str(r["id"]) for r in rows)
    fp = hashlib.sha1(("sslv:" + ",".join(ids)).encode("utf-8")).hexdigest()
    result: dict = {"source_id": src["id"], "name": src["name"], "scanned_at": scanned_at.isoformat(),
                    "unit_folders": len(rows), "price_files": 0, "changed": fp != src.get("last_fingerprint"), "fingerprint": fp,
                    "intake_id": None, "skipped": None, "web_recipe": None, "list_only": False,
                    "other_addresses": _other_addresses(src)}
    if not rows:
        result["skipped"] = "nav jaunu ss.lv sludinājumu ar šo numuru šajā ēkā"
        result["changed"] = False
        return result
    if not result["changed"] and not force:
        return result
    supersede = _pending_prev(src)
    links = "\n".join(f"- #{r['id']} {r.get('street') or ''} · {r.get('area_m2') or '?'} m² · {r.get('floor') or '?'} · "
                      f"{r.get('price') or '?'} € · https://www.{r['link']}" if r.get("link") and not str(r["link"]).startswith("http")
                      else f"- #{r['id']} {r.get('street') or ''} · {r.get('area_m2') or '?'} m² · {r.get('floor') or '?'} · {r.get('price') or '?'} € · {r.get('link') or ''}"
                      for r in rows)
    body = (f"PARTNERA SS.LV SLUDINĀJUMI — {src['name']} ({', '.join(src.get('phones') or [])}), ēkas: "
            f"{'; '.join(a.get('bp_address') or a.get('street') or '' for a in _src_addresses(src))}, "
            f"pēdējās {src.get('days') or SSLV_DEFAULT_DAYS} dienas ({scanned_at.strftime('%Y-%m-%d')}).\n"
            f"Katrs sludinājums = viena telpa; apstiprinot tas tiek PĀRŅEMTS (nevis izveidots no jauna) un publicēts mājaslapā.\n\n{links}\n")
    parsed = {
        "message_id": f"sslv:{src['id']}:{fp[:12]}",
        "forwarder_name": src.get("name"), "forwarder_email": src.get("agent_email") or "raimonds@rgcommerce.lv",
        "subject": f"{src['name']} — ss.lv sludinājumi {scanned_at.strftime('%Y-%m-%d')}",
        "body": body,
        "orig_sender": {"name": src.get("contact_name") or src.get("name"), "email": src.get("contact_email") or ""},
        "attachments_raw": [], "date": scanned_at.isoformat(),
    }
    meta = partner_intake._store_intake(parsed, "sslv")
    meta["drive_source_id"] = src["id"]
    meta["drive_fingerprint"] = fp
    meta["sslv_listing_ids"] = ids
    meta["prebuilt_analysis"] = _sslv_analysis(src, rows)
    d = partner_intake.INTAKE_DIR / meta["intake_id"]
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    # ss.lv galerijas kandidāti bildēm — tie paši sludinājumi (bez e-pasta linku meklēšanas)
    cands = [{"inbox_id": int(r["id"]), "street": r.get("street"), "city": r.get("city"),
              "area_m2": str(r["area_m2"]) if r.get("area_m2") else None, "floor": str(r["floor"]) if r.get("floor") else None,
              "price": str(r["price"]) if r.get("price") else None,
              "link": (r["link"] if str(r.get("link") or "").startswith("http") else f"https://www.{r['link']}") if r.get("link") else None,
              "is_agent": False, "phone": r.get("phone_numbers"), "last_updated": None,
              "same_room_score": 1.5, "floor_ok": True, "from_email": True, "gallery_n": int(r.get("n_raw") or 0)}
             for r in rows if r.get("link")]
    (d / "sslv_links.json").write_text(json.dumps(cands, ensure_ascii=False, indent=1), encoding="utf-8")
    result["intake_id"] = meta["intake_id"]
    rep = partner_intake.process_intake(meta["intake_id"], do_notify=False)
    result["summary"] = rep.get("summary")
    _finish_scan_intake(meta["intake_id"], rep, supersede, src, result)
    return result


def scan_source(src: dict, force: bool = False) -> dict:
    """Viens avots: mapes saraksts + cenu faili → nospiedums → ja mainījies (vai force) → ieplūde."""
    if (src.get("kind") or "drive") == "web":
        return scan_web_source(src, force=force)
    if src.get("kind") == "sslv":
        return scan_sslv_source(src, force=force)
    import link_media
    import partner_intake
    scanned_at = _now()
    entries = link_media.list_gdrive_folder(src["folder_id"], depth=MAX_FOLDER_DEPTH)
    if not entries:
        raise RuntimeError("Drive mape tukša vai nav pieejama (vai ir publiska?)")
    price_blobs: dict[str, bytes] = {}
    for fid in src.get("price_file_ids") or []:
        try:
            data = link_media.download_gdrive_file(fid)
        except Exception as e:
            logger.warning("partner_drive %s: cenu fails %s: %s", src["name"], fid, e)
            data = None
        if data:
            price_blobs[fid] = data
    fp = _fingerprint(entries, price_blobs)
    result: dict[str, Any] = {"source_id": src["id"], "scanned_at": scanned_at.isoformat(),
                              "folders": sum(1 for e in entries if e.get("kind") == "folder"),
                              "files": sum(1 for e in entries if e.get("kind") != "folder"),
                              "unit_folders": len(_unit_folders(entries)), "price_files": len(price_blobs),
                              "changed": fp != src.get("last_fingerprint"), "fingerprint": fp, "intake_id": None, "skipped": None}
    if not result["changed"] and not force:
        return result
    # iepriekšējais vēl gaida apstiprināšanu, bet avots mainījies → jaunais to AIZSTĀJ (vecais = «superseded», panelī
    # pazūd; Realto 2026-10-27: pēc labojuma «Skenēt no jauna» neko nedeva, jo vecais nolasījums stāvēja ceļā)
    supersede = _pending_prev(src)
    atts = []
    for fid, data in price_blobs.items():
        name = f"cenas_{fid[:8]}.pdf" if data[:5] == b"%PDF-" else f"cenas_{fid[:8]}.bin"
        if data[:4] == b"PK\x03\x04":
            name = f"cenas_{fid[:8]}.xlsx"
        atts.append({"name": name, "type": "application/pdf" if name.endswith(".pdf") else "application/octet-stream", "data": data})
    parsed = {
        "message_id": f"drive:{src['id']}:{fp[:12]}",
        "forwarder_name": src.get("name"), "forwarder_email": src.get("agent_email") or "raimonds@rgcommerce.lv",
        "subject": f"{src['name']} — Drive skenējums {scanned_at.strftime('%Y-%m-%d')}",
        "body": _body_text(src, entries, scanned_at),
        "orig_sender": {"name": src.get("contact_name") or src.get("name"), "email": src.get("contact_email") or ""},
        "attachments_raw": atts, "date": scanned_at.isoformat(),
    }
    meta = partner_intake._store_intake(parsed, "drive")
    meta["drive_source_id"] = src["id"]
    meta["drive_fingerprint"] = fp
    (partner_intake.INTAKE_DIR / meta["intake_id"] / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    result["intake_id"] = meta["intake_id"]
    rep = partner_intake.process_intake(meta["intake_id"], do_notify=False)
    result["summary"] = rep.get("summary")
    _finish_scan_intake(meta["intake_id"], rep, supersede, src, result)
    return result


def _actionable(report: dict) -> dict:
    """Vai automātiskā skenējuma ieplūdē ir KAS darāms: jaunas telpas, cenas/atjaunošanas maiņas, mūsu telpas, kas
    partnera sarakstā vairs nav (= iznomāt). Ja nekā — ieplūde ir troksnis (Raimonds 2026-10-28: «katru dienu ko mēs
    tur būdīsim; apstiprināšanai jānāk tikai jauniem vai cenas maiņām»)."""
    new = updates = missing = 0
    for a in report.get("addresses") or []:
        for u in a.get("units") or []:
            if u.get("status") != "match":
                new += 1
            elif u.get("decision") == "update":
                updates += 1
        missing += sum(1 for m in a.get("missing_in_list") or [] if m.get("default_checked", True))
    return {"new": new, "updates": updates, "missing": missing, "any": bool(new or updates or missing)}


def _finish_scan_intake(intake_id: str, report: dict, supersede: Optional[str], src: dict, result: dict) -> None:
    """Pēc apstrādes: ja ir ko darīt → Telegram + vecā neapstiprinātā ieplūde aizstāta; ja nav → ieplūde «nochange»
    (sarakstā neparādās, paziņojuma nav), vecā paliek."""
    import partner_intake
    act = _actionable(report)
    result["actionable"] = act
    if act["any"]:
        try:
            report["telegram_sent"] = partner_intake.notify(report)
        except Exception as e:
            logger.warning("partner_drive: notify %s: %s", intake_id, e)
        _mark_superseded(supersede, intake_id, src["id"])
        return
    mp = partner_intake.INTAKE_DIR / intake_id / "meta.json"
    try:
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta["status"] = "nochange"
        meta["nochange_at"] = _now().isoformat()
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning("partner_drive: nochange %s: %s", intake_id, e)
    result["skipped"] = "nav izmaiņu — viss jau DB (ieplūde paslēpta)"


def _pending_prev(src: dict) -> Optional[str]:
    """Avota iepriekšējā ieplūde, ja tā vēl nav apstiprināta (new/ready) — to aizstās jaunā."""
    import partner_intake   # lokāls imports (cikliska atkarība) — bez tā NameError (Realto Hausmaņa 2026-10-27)
    prev = src.get("last_intake_id")
    if not prev:
        return None
    mp = partner_intake.INTAKE_DIR / prev / "meta.json"
    if not mp.is_file():
        return None
    try:
        st = json.loads(mp.read_text(encoding="utf-8")).get("status")
    except Exception:
        return None
    return prev if st in ("new", "ready") else None


def _mark_superseded(old_id: Optional[str], new_id: str, source_id: Optional[str] = None) -> None:
    """Vecā neapstiprinātā ieplūde (un visas citas šī avota new/ready ieplūdes, kas palikušas no agrākiem skenējumiem)
    → «superseded»: sarakstā pazūd, jaunākais nolasījums ir vienīgais."""
    import partner_intake
    cands = {old_id} if old_id else set()
    if source_id and partner_intake.INTAKE_DIR.is_dir():
        for d in partner_intake.INTAKE_DIR.iterdir():
            if (d / "meta.json").is_file():
                cands.add(d.name)
    for iid in cands:
        if not iid or iid == new_id:
            continue
        mp = partner_intake.INTAKE_DIR / iid / "meta.json"
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            if meta.get("status") not in ("new", "ready"):
                continue
            if iid != old_id and meta.get("drive_source_id") != source_id:
                continue
            meta["status"] = "superseded"
            meta["superseded_by"] = new_id
            mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning("partner_drive: superseded %s: %s", iid, e)


def _apply_result(src: dict, result: dict, error: Optional[str] = None) -> None:
    sources = load_sources()
    for s in sources:
        if s.get("id") == src["id"]:
            s["last_scan_at"] = result.get("scanned_at") or _now().isoformat()
            s["last_result"] = {k: result.get(k) for k in ("changed", "intake_id", "skipped", "unit_folders", "price_files", "summary", "other_addresses")}
            s["last_error"] = error
            if result.get("web_recipe"):
                s["web_recipe"] = result["web_recipe"]
            if result.get("intake_id") or (not result.get("changed") and not error):
                # nospiedumu atjauno tikai, ja ieplūde izveidota vai nekas nav mainījies; «skipped» → rīt vēlreiz
                s["last_fingerprint"] = result.get("fingerprint") or s.get("last_fingerprint")
            if result.get("intake_id"):
                s["last_intake_id"] = result["intake_id"]
    save_sources(sources)


def _set_scanning(sid: str, on: bool) -> None:
    sources = load_sources()
    for s in sources:
        if s.get("id") == sid:
            s["scanning"] = on
            if on:
                s["scanning_since"] = _now().isoformat()
    save_sources(sources)


def run_scan(sid: str, force: bool = False) -> dict:
    src = next((s for s in load_sources() if s.get("id") == sid), None)
    if src is None:
        raise ValueError("Nav tāda avota")
    _set_scanning(sid, True)
    try:
        res = scan_source(src, force=force)
    except Exception as e:
        _apply_result(src, {"scanned_at": _now().isoformat()}, error=f"{type(e).__name__}: {str(e)[:200]}")
        _set_scanning(sid, False)
        raise
    _apply_result(src, res)
    _set_scanning(sid, False)
    return res


_scan_threads: dict[str, "threading.Thread"] = {}


def start_scan(sid: str, force: bool = False) -> dict:
    """Panelim: skenēšana FONĀ (mapju bildes ~400 MB var ilgt 10–30 min; HTTP nedrīkst gaidīt).
    Panelis redz `scanning` avotu sarakstā un pēc tam `last_result`."""
    import threading
    src = next((s for s in load_sources() if s.get("id") == sid), None)
    if src is None:
        raise ValueError("Nav tāda avota")
    t = _scan_threads.get(sid)
    if t is not None and t.is_alive():
        return {"started": False, "already_running": True}
    t = threading.Thread(target=lambda: _safe_run(sid, force), name=f"drive-scan-{sid}", daemon=True)
    _scan_threads[sid] = t
    t.start()
    return {"started": True}


def _safe_run(sid: str, force: bool) -> None:
    try:
        run_scan(sid, force=force)
    except Exception as e:
        logger.error("partner_drive %s: %s", sid, e)


def maybe_scan_all() -> list[dict]:
    """Sauc partner_intake.run_loop katrā ciklā: skenē avotus, kam pienācis laiks (daily/weekly)."""
    out = []
    for src in load_sources():
        if not src.get("enabled", True):
            continue
        t = _scan_threads.get(src["id"])
        if t is not None and t.is_alive():
            continue   # panelis jau skenē šo avotu
        last = src.get("last_scan_at")
        due = True
        if last:
            try:
                due = _now() - datetime.fromisoformat(last) >= SCHEDULES.get(src.get("schedule") or "daily", SCHEDULES["daily"])
            except Exception:
                due = True
        if not due:
            continue
        try:
            res = run_scan(src["id"])
            out.append(res)
            logger.info("partner_drive %s: %s", src["name"], {k: res.get(k) for k in ("changed", "intake_id", "skipped")})
        except Exception as e:
            logger.error("partner_drive %s: %s", src.get("name"), e)
    return out
