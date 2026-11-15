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
    # NE read_only: vajag redzēt SLĒPTĀS rindas — tās partneris ir paslēpis (vecas/dublētas; Realto 167 slēptas rindas,
    # P. Brieža 43 telpas slēptajās rindās vēlreiz — Raimonds 2026-11-15). Ņemam tikai redzamās.
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    out: list[dict] = []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        cols: dict[str, int] = {}
        for ri, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if cols and ws.row_dimensions[ri].hidden:
                continue
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
    # tabulā viena un tā pati rinda mēdz būt divreiz (Realto P. Brieža 43: 4 telpas × 2) → vienreiz
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for r in out:
        k = tuple(str(r.get(x) or "").strip().lower() for x in ("street_raw", "part", "floor", "room", "area", "func", "rate", "total", "status"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


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
    # StockOfiss = birojs + NOLIKTAVA; «birojs / darbnīca» ir birojs (Raimonds 2026-11-15: «nekāda sakara ar stock ofisu»)
    if "biroj" in f and "noliktav" in f:
        return "StockOfiss"
    if f.startswith("biroj"):
        return "Birojs"
    # galvenā ir PIRMĀ funkcija («noliktava/darbnīca» = Noliktava, ne Ražošana)
    first = re.split(r"[/,;]", f)[0].strip()
    for part in (first, f):
        for pats, t in TYPE_RULES:
            if any(p in part for p in pats):
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
    try:
        facts_list = notes_to_facts([r.get("notes") or "" for r in rows])
    except Exception as e:
        logger.warning("partner_sheet: fakti: %s", e)
        facts_list = [{} for _ in rows]
    for r, facts in zip(rows, facts_list):
        kind, why = classify(r)
        if kind == "skip":
            skipped.append({"item": _row_label(r), "reason": why, "status": r.get("status") or "",
                            "ref": " ".join(x for x in (r.get("part"), r.get("room")) if x) or None})
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
        if facts:
            u["facts"] = facts   # «Par telpu» priekšaizpilde (panelis); apstiprinot iet uz listingu kā aģenta fakti
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
    # ēka, kurai bija mūsu ieplūde, tabulā pazudusi pavisam → tukša grupa (reconcile piedāvās mūsu telpas kā iznomātas)
    for key, st in state.items():
        if key not in groups and st.get("intake_id") and st.get("fp") != "gone":
            city = key.split("|")[-1].title() if "|" in key else "Rīga"
            groups[key] = {"street": st.get("street") or key.split("|")[0], "city": "Rīga" if city.lower() == "rīga" else city,
                           "district": None, "rows": [], "gone": True}
    for key, g in groups.items():
        fp = "gone" if g.get("gone") else _fp(g["rows"])
        st = state.get(key) or {}
        if fp == st.get("fp") and not force:
            continue
        if fp == st.get("fp") and _pending(st.get("intake_id")) and not force:
            continue   # tas pats, vēl gaida apstiprināšanu («Skenēt tagad» ar force — pārbūvē, piem. pēc koda labojuma)
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
            # jaunajā nolasījumā nav ko darīt → arī VECĀ neapstiprinātā šīs ēkas ieplūde vairs nav aktuāla (Realto 2026-11-15:
            # pēc slēpto rindu izmešanas Murjāņu 91 / Atlasa 8 vecās ieplūdes palika rindā ar neesošām telpām)
            if supersede:
                partner_drive._mark_superseded(supersede, meta["intake_id"], None)
            mp = partner_intake.INTAKE_DIR / meta["intake_id"] / "meta.json"
            m2 = json.loads(mp.read_text(encoding="utf-8"))
            m2["status"] = "nochange"
            m2["nochange_at"] = scanned_at.isoformat()
            mp.write_text(json.dumps(m2, ensure_ascii=False, indent=1), encoding="utf-8")
        state[key] = {"fp": fp, "intake_id": meta["intake_id"] if act["any"] else (None if supersede else st.get("intake_id")),
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


# ── piezīmes → «Par telpu» fakti (Raimonds 2026-11-15: «nav siltuma» → Apkure: Nav, «kabinets ar WC» → WC) ──
NOTE_FACT_FIELDS: dict[str, Any] = {
    "Apkure": ["Centrālā", "Gāzes", "Elektriskā", "Nav"],
    "cik_WC": ["1 WC telpā", "2 WC telpā", "3 WC telpā", "4 WC telpā", "1 WC koplietošanā", "2 WC koplietošanā", "Nav"],
    "Space_condition": ["Jauns", "Labs", "Lietots", "Nepabeigts", "Nepieciešams remonts", "Kapitālais remonts"],
    "Planojums": ["Atvērts", "Zāle + palīgtelpas", "Kabinetu", "Sekciju"],
    "Mebeleta_telpa": ["Jā", "Nē", "Daļēji"],
    "Logu_type": ["Lielie Logi", "Standarta logi", "Maz logu", "Nav logi"],
    "Virtuve_check": "check", "Ir_izlietne_telpa_check": "check", "Sava_ieeja_check": "check", "street_entrance": "check",
    "Ventilacijas_sistema_check": "check", "Pacelamie_varti_check": "check", "Rampa_logistikai_check": "check",
    "Cik_telpas": "num", "Griestu_augstums": "num", "electric_power_kw": "num",
}
NOTES_MODEL_DEFAULT = "gpt-4o-mini"


def _facts_cache_path():
    import partner_intake
    return partner_intake.INTAKE_DIR / "_sheet_facts_cache.json"


def _facts_schema() -> dict:
    props: dict = {"i": {"type": "integer"}}
    for k, spec in NOTE_FACT_FIELDS.items():
        if spec == "check":
            props[k] = {"type": ["string", "null"], "enum": ["checked", "not checked", None]}
        elif spec == "num":
            props[k] = {"type": ["number", "null"]}
        else:
            props[k] = {"type": ["string", "null"], "enum": list(spec) + [None]}
    item = {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}
    return {"type": "object", "additionalProperties": False, "required": ["items"],
            "properties": {"items": {"type": "array", "items": item}}}


# AI drīkst atzīmēt lauku TIKAI, ja piezīmē ir atbilstošs vārds (pret izdomājumiem: «3 fāz. el.» ≠ 3 kW, mēbeles bez «mēbel»)
FACT_GUARDS: dict[str, str] = {
    "Apkure": r"apkur|siltum|radiator|gāz|gaz|elektr.*sild",
    "cik_WC": r"\bwc\b|tualet|sanmezgl|sanitār",
    "Space_condition": r"remont|stāvokl|stavokl|renov|jaun|nolietot|apdar|kosm",
    "Planojums": r"kabinet|atvērt|atvert|open|zāle|zale|sekcij|telpas|telpām",
    "Mebeleta_telpa": r"mēbel|mebel",
    "Logu_type": r"log|gaiš|gais",
    "Virtuve_check": r"virtuv",
    "Ir_izlietne_telpa_check": r"izlietn|ūdens|udens",
    "Sava_ieeja_check": r"ieej",
    "street_entrance": r"ieej.*iel|no ielas",
    "Ventilacijas_sistema_check": r"ventil",
    "Pacelamie_varti_check": r"vārt|vart",
    "Rampa_logistikai_check": r"ramp",
    "Cik_telpas": r"\d|div|trīs|tris|četr|cetr|piec|telp|kabinet",
    "Griestu_augstums": r"griest",
    "electric_power_kw": r"kw\b|kilovat",
}


def _guard(note: str, facts: dict) -> dict:
    low = (note or "").lower()
    out = {}
    for k, v in facts.items():
        g = FACT_GUARDS.get(k)
        if g and not re.search(g, low):
            continue
        if k == "Apkure" and v == "Centrālā" and "centr" not in low:
            continue
        if k == "Space_condition" and v == "Jauns" and not re.search(r"\bjaun", low):
            v = "Labs"
        if k == "Logu_type" and v == "Nav logi" and re.search(r"no kuriem|daļ|dal[aē]|dažiem|dažas|vairāki|atsevišķ|ir bez logiem", low):
            v = "Maz logu"   # tikai daļai kabinetu nav logu (Zaļā 2026-11-15: «9 kabineti, no kuriem seši bez logiem»)
        if k == "cik_WC":
            continue   # WC nosaka tikai pēc vārda zemāk (AI te bieži kļūdās: «6 kabineti, wc» → «Nav»)
        if k == "Planojums" and not ((v == "Kabinetu" and "kabinet" in low) or (v == "Atvērts" and re.search(r"atvērt|atvert|open", low))
                                     or (v == "Zāle + palīgtelpas" and re.search(r"zāl|zal", low)) or (v == "Sekciju" and "sekcij" in low)):
            continue
        if k == "Cik_telpas":
            words = {"div": 2, "trīs": 3, "tris": 3, "četr": 4, "cetr": 4, "piec": 5, "seš": 6, "ses": 6, "septiņ": 7, "astoņ": 8, "deviņ": 9}
            nums = {int(x) for x in re.findall(r"(\d{1,2})\s*(?:kabinet|telp|istab)", low)}
            nums |= {val for w, val in words.items() if re.search(w + r"\w*\s+(?:kabinet|telp|istab)", low)}
            try:
                if int(float(v)) not in nums:
                    continue
            except Exception:
                continue
        out[k] = v
    # WC — AI to bieži izlaiž; skaidrs vārds piezīmē = fakts («kabinets ar WC» → 1 WC telpā; «2 WC» → 2; «WC izbūve» = vēl nav)
    if "cik_WC" not in out and re.search(r"\bwc\b", low) and not re.search(r"wc\s+izb[ūu]v|iekļaut\w*\s+k[āa]pņu", low):
        m = re.search(r"\b([2-4])\s*wc\b", low)
        shared = "koplieto" in low
        out["cik_WC"] = f"{m.group(1) if m else 1} WC {'koplietošanā' if shared else 'telpā'}" if not (shared and m and int(m.group(1)) > 2) else "2 WC koplietošanā"
    return out


def notes_to_facts(notes: list[str]) -> list[dict]:
    """Piezīmju teksti → fakti katrai (tikai tas, kas SKAIDRI rakstīts; nezināms = null). Kešs pēc teksta — AI
    lasa tikai jaunas/mainītas piezīmes (gpt-4o-mini, visa Realto tabula ≈ daži centi)."""
    import os
    cache_p = _facts_cache_path()
    try:
        cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.is_file() else {}
    except Exception:
        cache = {}
    keyf = lambda t: hashlib.sha1(t.strip().lower().encode("utf-8")).hexdigest()[:16]   # noqa: E731
    todo = sorted({n.strip() for n in notes if n and n.strip() and keyf(n) not in cache})
    api_key = os.getenv("OPENAI_API_KEY")
    if todo and api_key:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        model = os.getenv("SHEET_NOTES_MODEL") or NOTES_MODEL_DEFAULT
        for i0 in range(0, len(todo), 40):
            chunk = todo[i0:i0 + 40]
            listing = "\n".join(f"{i}. {t}" for i, t in enumerate(chunk))
            prompt = ("Tās ir komercīpašumu telpu piezīmes no iznomātāja tabulas (latviski, saīsinājumi). Katrai piezīmei (i = numurs) "
                      "aizpildi TIKAI to, kas tajā SKAIDRI pateikts; viss pārējais null. Piemēri: «nav siltuma»/«bez apkures» → Apkure «Nav»; "
                      "«ar WC», «WC telpā» → cik_WC «1 WC telpā»; «WC koplietošanā» → «1 WC koplietošanā»; «remonts nepieciešams», «kosm.remonts» "
                      "→ Space_condition «Nepieciešams remonts»; «labs stāvoklis», «renovēts», «izremontēts» → «Labs» (NE «Jauns»); «Apkure.» (ir apkure, "
                      "tips nav teikts) → Apkure null; tikai «centrālā apkure» → «Centrālā»; «4 kabineti» → Cik_telpas 4, Planojums "
                      "«Kabinetu»; «izlietne» → Ir_izlietne_telpa_check «checked»; «virtuve» → Virtuve_check «checked»; «sava ieeja» → "
                      "Sava_ieeja_check; «ieeja no ielas» → street_entrance; «vārti» → Pacelamie_varti_check; «rampa» → Rampa_logistikai_check; "
                      "«gaiša telpa, lieli logi» → Logu_type «Lielie Logi»; «bez logiem» → «Nav logi»; «ar mēbelēm» → Mebeleta_telpa «Jā». "
                      "NEminē: ja piezīmē nekas par lauku nav teikts, tas ir null.\n\n" + listing)
            try:
                resp = client.responses.create(
                    model=model, input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                    text={"format": {"type": "json_schema", "name": "facts", "schema": _facts_schema(), "strict": True}})
                data = json.loads(resp.output_text)
                for it in data.get("items") or []:
                    i = it.get("i")
                    if isinstance(i, int) and 0 <= i < len(chunk):
                        cache[keyf(chunk[i])] = {k: v for k, v in it.items() if k != "i" and v not in (None, "")}
            except Exception as e:
                logger.warning("partner_sheet: piezīmju fakti (%d): %s", len(chunk), e)
                break
        try:
            cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    out = []
    for n in notes:
        f = _guard(n, dict(cache.get(keyf(n)) or {})) if n and n.strip() else {}
        for k, v in list(f.items()):
            if isinstance(v, float) and v.is_integer():
                f[k] = int(v)
            if NOTE_FACT_FIELDS.get(k) == "num":
                f[k] = str(f[k])
        out.append(f)
    return out
