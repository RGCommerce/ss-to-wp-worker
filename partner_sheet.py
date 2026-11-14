"""Partnera brīvo telpu REĢISTRS Google Sheets tabulā (Realto 2026-11-14: ~500 telpas 36 adresēs, atjauno katru mēnesi).

Princips tāds pats kā Drive/ss.lv skenējumam (partner_drive): skenē pēc grafika BEZ AI (tabula ir strukturēta),
katrai ADRESEI savs nospiedums → ieplūde tikai tām ēkām, kur kaut kas mainījies; katra ēka = atsevišķa ieplūde,
ko aģents apstiprina pats. Ņem tikai BRĪVAS telpas ar cenu; rezervētās, «jāprecizē», tehniskās, iznomātās, bez
cenas un dzīvokļus/kopmītnes ieliek `skipped_items` (parādās kā izlaistie, telpas nerada). Mūsu telpas, kuru
tabulā nav, par iznomātām NEpiedāvā (reģistrs nav dzīvs saraksts; Raimonds 2026-11-14).

Avots: partner_drive avots ar kind="sheet" un `url` = Google Sheets saite (publiska; lasa /export?format=xlsx).
Stāvoklis pa adresēm: src["sheet_state"] = {adreses_atslēga: {"fp", "intake_id", "street"}}.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("partner_sheet")

FREE_RE = re.compile(r"^\s*br[īi]v", re.I)
AVAIL_RE = re.compile(r"no\s+(\d{1,2}\.\d{1,2}\.\d{4})", re.I)
# funkcijas, ko komercīpašumu DB nevajag (Raimonds 2026-11-14: «dzīvoklis nav vajadzīgs»)
SKIP_FUNC = ("dzīvok", "dzivok", "kopmītn", "kopmitn", "viesnīc", "viesnic", "transformator", "gaiten", "ārtelp", "artelp", "nojume")
# funkcija / piezīmes → mūsu telpu grupa (secība svarīga: «birojs/noliktava» = StockOfiss pirms «birojs»)
TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("stock office", "stock-office", "stockofiss"), "StockOfiss"),
    (("kafejn", "ēdināšan", "edinasan", "restorān", "restoran"), "Restorans/Cafe"),
    (("sporta z",), "Sporta zāle"),
    (("māksl", "maksl", "studij"), "Studija"),
    (("tirdzniec", "veikal", "pakalpojum"), "Tirdzniecība"),
    (("ražošan", "razosan", "darbnīc", "darbnic"), "Ražošana"),
    (("noliktav", "palīgtelp", "paligtelp", "pagrab", "garāž", "garaz", "arhīv", "arhiv"), "Noliktava"),
    (("biroj", "kabinet"), "Birojs"),
]
RIGA_SUBURB_CITIES = ("saulkrasti", "sigulda", "jūrmala", "jurmala", "ogre", "salaspils", "ķekava", "kekava", "mārupe", "marupe",
                      "olaine", "babīte", "babite", "ādaži", "adazi")
HEADER_KEYS = {"loc": ("atrašanās", "atrasanas"), "street": ("adrese",), "part": ("korpuss", "ēka /", "eka /"),
               "floor": ("stāvs", "stavs"), "room": ("telpa",), "area": ("platība", "platiba"), "func": ("funkcij",),
               "rate": ("eur/m",), "total": ("eur/mēn", "eur/men"), "notes": ("piezīm", "piezim"), "status": ("status",)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sheet_id(url: str) -> Optional[str]:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", url or "")
    return m.group(1) if m else None


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def download_xlsx(url: str) -> bytes:
    import requests
    sid = sheet_id(url)
    if not sid:
        raise ValueError("Google Sheets saite nav atpazīta")
    r = requests.get(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    if r.content[:4] != b"PK\x03\x04":
        raise RuntimeError("Tabula nav pieejama publiski (eksports neatdeva xlsx)")
    return r.content


def sheet_rows(data: bytes) -> list[dict]:
    """Visas datu rindas no lapām, kurās ir galvene ar «Adrese» un «Platība» (kolonnas pēc nosaukumiem, ne pozīcijām)."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out: list[dict] = []
    for ws in wb.worksheets:
        cols: dict[str, int] = {}
        for row in ws.iter_rows(values_only=True):
            if not cols:
                low = [str(c or "").strip().lower() for c in row]
                if any("adrese" in c for c in low) and any("plat" in c for c in low):
                    for key, pats in HEADER_KEYS.items():
                        for i, c in enumerate(low):
                            if c and any(p in c for p in pats) and key not in cols:
                                cols[key] = i
                                break
                continue
            g = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None   # noqa: E731
            street = _txt(g("street"))
            area = _num(g("area"))
            if not street or not area:
                continue
            out.append({"loc": _txt(g("loc")), "street_raw": street, "part": _txt(g("part")), "floor": _txt(g("floor")),
                        "room": _txt(g("room")), "area": area, "func": _txt(g("func")), "rate": _num(g("rate")),
                        "total": _num(g("total")), "notes": _txt(g("notes")), "status": _txt(g("status")), "sheet": ws.title})
    return out


def split_address(street_raw: str, loc: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """«Krustpils iela 17, Rīga» → (Krustpils iela 17, Rīga, None, None); «Pētersalas 1B / P. Brieža 43 / …» → pirmā;
    «Braslas 22 D» → (Braslas 22, …, korpuss «D») — kompleksa korpusi ir viena adrese ar daļām;
    atrašanās vieta Saulkrasti/Sigulda → pilsēta; citādi Rīga + rajons."""
    s = re.sub(r",\s*r[īi]ga\s*$", "", street_raw.strip(), flags=re.I).strip()
    s = s.split(" / ")[0].replace('"', "").strip().strip(",")
    s = re.sub(r"\s+", " ", s)
    corp = None
    m = re.match(r"^(.*\d+)\s+([A-Za-z])$", s)
    if m:
        s, corp = m.group(1), m.group(2).upper()
    city, district = "Rīga", None
    l = (loc or "").strip()
    if l.lower() in RIGA_SUBURB_CITIES:
        city = l
    elif "turaid" in s.lower() or "turaid" in l.lower():
        city = "Sigulda"
    elif l and not any(x in l.lower() for x in ("centrs", "kinostudija")):
        district = l
    return s, city, district, corp


def _space_type(func: str, notes: str, fallback: Optional[str]) -> Optional[str]:
    f = (func or "").lower()
    if "biroj" in f and ("noliktav" in f or "darbn" in f):
        return "StockOfiss"
    for pats, t in TYPE_RULES:
        if any(p in f for p in pats):
            return t
    n = (notes or "").lower()
    for pats, t in TYPE_RULES:
        if any(p in n for p in pats):
            return t
    return fallback


def _blank_unit() -> dict:
    import partner_intake
    props = partner_intake._schema()["properties"]["addresses"]["items"]["properties"]["units"]["items"]["properties"]
    u: dict = {}
    for k, spec in props.items():
        t = spec.get("type")
        types = t if isinstance(t, list) else [t]
        u[k] = [] if "array" in types else None if "null" in types else False if "boolean" in types else "" if "string" in types else None
    return u


def _row_label(r: dict) -> str:
    bits = [r["street_raw"], r.get("part"), f"{r.get('floor')}. st" if r.get("floor") else "", f"nr {r['room']}" if r.get("room") else "",
            f"{r['area']:g} m²"]
    return " · ".join(b for b in bits if b)


def classify(r: dict) -> tuple[str, Optional[str]]:
    """→ ("unit", None) | ("skip", iemesls)."""
    st = r.get("status") or ""
    f = (r.get("func") or "").lower()
    if any(p in f for p in SKIP_FUNC):
        return "skip", f"funkcija: {r.get('func')}"
    if not FREE_RE.match(st):
        return "skip", f"statuss: {st or 'nav'}"
    if r.get("rate") is None and r.get("total") is None:
        return "skip", "bez cenas"
    return "unit", None


def build_analysis(src: dict, rows: list[dict], street: str, city: str, district: Optional[str]) -> dict:
    """Vienas adreses prebuilt analīze (kā ss.lv numura avotam) — bez AI."""
    typed = [t for t in (_space_type(r["func"], "", None) for r in rows) if t]
    dominant = max(set(typed), key=typed.count) if typed else None
    units, skipped = [], []
    for r in rows:
        kind, why = classify(r)
        if kind == "skip":
            skipped.append({"item": _row_label(r), "reason": why})
            continue
        u = _blank_unit()
        area = float(r["area"])
        rate, total = r.get("rate"), r.get("total")
        if rate is None and total:
            rate = round(total / area, 2)
        if total is None and rate:
            total = round(area * rate, 2)
        m = AVAIL_RE.search(r.get("status") or "")
        ref = " ".join(x for x in (r.get("part"), r.get("room")) if x) or None
        u.update({
            "ref": ref, "building_part": r.get("part") or None, "floor": r.get("floor") or None,
            "area_m2": area, "area_approx": False, "space_type": _space_type(r["func"], r.get("notes") or "", dominant),
            "deal": "rent", "rent_per_m2_min": rate, "rent_total": total,
            "condition": ("jāremontē" if re.search(r"remont", (r.get("notes") or "").lower()) else None),
            "available_from": m.group(1) if m else "tagad",
            "features": [], "description_lv": "", "partner_notes": (r.get("notes") or "")[:400],
            "offer_type": "standard", "confidence": 1.0,
            "evidence": f"{src.get('name')} reģistrs: {_row_label(r)} · {r.get('func') or '?'} · {r.get('status')}",
            "attachment_hint": None, "page_hint": None, "link_ids": [],
        })
        units.append(u)
    return {
        "is_vacancy_offer": True,
        "partner": {"company": src.get("name"), "contact_name": src.get("contact_name") or None,
                    "email": src.get("contact_email") or None, "phone": src.get("contact_phone") or None, "website": None},
        "list_date": _now().strftime("%Y-%m-%d"), "looks_complete": False, "language": "lv",
        "addresses": [{"street": street, "city": city, "district": district, "building_name": None, "address_known": True,
                       "list_complete_here": False, "units": units}],
        "skipped_items": skipped, "warnings": [],
    }


def group_rows(rows: list[dict]) -> dict[str, dict]:
    import partner_intake
    groups: dict[str, dict] = {}
    for r in rows:
        street, city, district, corp = split_address(r["street_raw"], r.get("loc") or "")
        if corp:
            r["part"] = " ".join(x for x in (f"{corp} korpuss", r.get("part")) if x)
        key = f"{partner_intake._street_key(street)}|{city.lower()}"
        g = groups.setdefault(key, {"street": street, "city": city, "district": district, "rows": []})
        g["rows"].append(r)
    return groups


def _fp(rows: list[dict]) -> str:
    sig = sorted(json.dumps({k: r.get(k) for k in ("street_raw", "part", "floor", "room", "area", "func", "rate", "total", "notes", "status")},
                            ensure_ascii=False, sort_keys=True) for r in rows)
    return hashlib.sha1("\n".join(sig).encode("utf-8")).hexdigest()


def _body(src: dict, g: dict, scanned_at: datetime) -> str:
    lines = [f"PARTNERA REĢISTRS (Google Sheets) — {src.get('name')} · {g['street']}, {g['city']} ({scanned_at.strftime('%Y-%m-%d')}).",
             "Katra tabulas rinda = viena telpa; brīvās ar cenu → telpas; pārējās (rezervētas, jāprecizē, tehniskas, bez cenas, dzīvokļi) → izlaistas.",
             f"Avots: {src.get('url')}", ""]
    for r in g["rows"]:
        kind, why = classify(r)
        price = f"{r['rate']:g} €/m²" if r.get("rate") else (f"{r['total']:g} €/mēn" if r.get("total") else "bez cenas")
        lines.append(f"- {_row_label(r)} · {r.get('func') or '?'} · {price} · {r.get('status')}" + (f" → izlaista ({why})" if kind == "skip" else "")
                     + (f" · {r['notes']}" if r.get("notes") else ""))
    return "\n".join(lines) + "\n"


def _save_state(sid: str, state: dict) -> None:
    import partner_drive
    sources = partner_drive.load_sources()
    for s in sources:
        if s.get("id") == sid:
            s["sheet_state"] = state
    partner_drive.save_sources(sources)


def _pending(intake_id: Optional[str]) -> bool:
    import partner_intake
    if not intake_id:
        return False
    mp = partner_intake.INTAKE_DIR / intake_id / "meta.json"
    try:
        return json.loads(mp.read_text(encoding="utf-8")).get("status") in ("new", "ready")
    except Exception:
        return False


def scan_sheet_source(src: dict, force: bool = False) -> dict:
    import partner_drive
    import partner_intake
    scanned_at = _now()
    data = download_xlsx(src.get("url") or "")
    rows = sheet_rows(data)
    if not rows:
        raise RuntimeError("Tabulā nav atrasta neviena telpu rinda (vajag galveni «Adrese», «Platība»)")
    groups = group_rows(rows)
    state: dict = dict(src.get("sheet_state") or {})
    whole_fp = hashlib.sha1(data).hexdigest()
    result: dict[str, Any] = {"source_id": src["id"], "name": src["name"], "scanned_at": scanned_at.isoformat(),
                              "unit_folders": len(rows), "price_files": 0, "addresses": len(groups),
                              "changed": False, "fingerprint": whole_fp, "intake_id": None, "intakes": [], "skipped": None}
    created: list[tuple[str, dict]] = []
    for key, g in groups.items():
        fp = _fp(g["rows"])
        st = state.get(key) or {}
        if fp == st.get("fp") and not force:
            continue
        if fp == st.get("fp") and _pending(st.get("intake_id")):
            continue   # tas pats, vēl gaida apstiprināšanu
        analysis = build_analysis(src, g["rows"], g["street"], g["city"], g["district"])
        n_units = len(analysis["addresses"][0]["units"])
        if n_units == 0 and not st.get("intake_id"):
            state[key] = {"fp": fp, "intake_id": None, "street": g["street"], "units": 0, "at": scanned_at.isoformat()}
            continue   # ēkā nav nevienas brīvas telpas ar cenu — ieplūdi neveido
        supersede = st.get("intake_id") if _pending(st.get("intake_id")) else None
        parsed = {
            "message_id": f"sheet:{src['id']}:{key}:{fp[:10]}",
            "forwarder_name": src.get("name"), "forwarder_email": src.get("agent_email") or "raimonds@rgcommerce.lv",
            "subject": f"{src['name']} — {g['street']} ({scanned_at.strftime('%Y-%m-%d')})",
            "body": _body(src, g, scanned_at),
            "orig_sender": {"name": src.get("contact_name") or src.get("name"), "email": src.get("contact_email") or ""},
            "attachments_raw": [], "date": scanned_at.isoformat(),
        }
        meta = partner_intake._store_intake(parsed, "sheet")
        meta["drive_source_id"] = src["id"]
        meta["drive_fingerprint"] = fp
        meta["sheet_address_key"] = key
        meta["prebuilt_analysis"] = analysis
        (partner_intake.INTAKE_DIR / meta["intake_id"] / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        rep = partner_intake.process_intake(meta["intake_id"], do_notify=False)
        act = partner_drive._actionable(rep)
        if act["any"]:
            if supersede:
                partner_drive._mark_superseded(supersede, meta["intake_id"], None)   # tikai šīs adreses vecā
            created.append((meta["intake_id"], rep))
        else:
            mp = partner_intake.INTAKE_DIR / meta["intake_id"] / "meta.json"
            m2 = json.loads(mp.read_text(encoding="utf-8"))
            m2["status"] = "nochange"
            m2["nochange_at"] = scanned_at.isoformat()
            mp.write_text(json.dumps(m2, ensure_ascii=False, indent=1), encoding="utf-8")
        state[key] = {"fp": fp, "intake_id": meta["intake_id"] if act["any"] else st.get("intake_id"),
                      "street": g["street"], "units": n_units, "at": scanned_at.isoformat()}
        result["intakes"].append({"intake_id": meta["intake_id"], "street": g["street"], "units": n_units,
                                  "summary": rep.get("summary"), "actionable": act["any"]})
    _save_state(src["id"], state)
    result["changed"] = bool(created)
    result["intake_id"] = created[-1][0] if created else None
    result["summary"] = {"intakes": len(created), "addresses": len(groups), "rows": len(rows)}
    if created:
        _notify(src, created)
    else:
        result["skipped"] = "nav izmaiņu nevienā ēkā"
    return result


def _notify(src: dict, created: list[tuple[str, dict]]) -> int:
    """Viens Telegram par visu skenējumu (ne 36 atsevišķi)."""
    import partner_intake
    if not partner_intake.DATABASE_URL:
        return 0
    try:
        import psycopg
        from psycopg.rows import dict_row
        lines = [f"🟢 <b>{html_mod.escape(str(src.get('name')))}</b> reģistrs: {len(created)} ēkas apstiprināšanai"]
        for iid, rep in created[:12]:
            s = rep.get("summary") or {}
            a = (rep.get("addresses") or [{}])[0]
            lines.append(f"• {html_mod.escape(str(a.get('street') or '?'))} — {s.get('units', 0)} telpas (jaunas {s.get('new', 0)}, sakrīt {s.get('match', 0)}) "
                         f"{partner_intake.PANEL_URL}/partner-intake/{iid}")
        if len(created) > 12:
            lines.append(f"… un vēl {len(created) - 12}: {partner_intake.PANEL_URL}/partner-intake")
        html = "\n".join(lines)
        with psycopg.connect(partner_intake.DATABASE_URL, row_factory=dict_row) as conn:
            subs = conn.execute("SELECT agent_email, chat_id FROM properties.telegram_subscribers WHERE active IS TRUE AND chat_id IS NOT NULL").fetchall()
        fwd = (src.get("agent_email") or "").lower()
        targets = [r for r in subs if (r["agent_email"] or "").lower() == fwd] or subs
        return sum(1 for r in targets if partner_intake._telegram_send(str(r["chat_id"]), html))
    except Exception as e:
        logger.warning("partner_sheet: notify %s: %s", src.get("name"), e)
        return 0
