"""land_ai.py — zemes (zeme) AI prompts, šablons un apraksta salikšana.

Koplietots ss-to-wp-worker pusē (agent_ai_poller = anketas ceļš). Spoguļo
test-runner-db/test_runner_db.py zemes loģiku (tāpat kā PROMPT tiek spoguļots
starp test_runner_db un ai_text_helpers). Zeme NEiet caur komerc-promptu.

Lietojums (anketa):
    import land_ai
    if land_ai.is_land_row(row):
        result = land_ai.analyze_land(client, MODEL, url, text, image_urls)
        result["land_description"] = land_ai.build_land_description(row, result)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


LAND_PROMPT = r"""
# SS.LV ZEMES (PLOT) SLUDINĀJUMA ANALĪZE — ŠABLONA REŽĪMS

## GOAL
Analyze one LAND (zeme) listing and fill the structured fields using BOTH the
listing text AND all images. You are a robot: fixed structure, no free prose.

## HARD RULES
0. READ THE ENTIRE listing text to the end. The zoning code (JC8, DzM1, R, ...) and
   utilities are OFTEN stated lower in the text, not at the top.
1. Do NOT invent. Fill a field ONLY if the text or an image supports it; otherwise
   "Nav minēts" (or [] for communications).
2. Use ONLY allowed enum values. Output MUST match the JSON schema exactly. No markdown.
3. Never output commercial-premises fields (ceiling, heating, WC, floor). Do NOT output
   address, price, phone or agent name.
4. Images are evidence: power lines/poles = elektrība; visible pipes/manholes/hydrants =
   ūdens/kanalizācija/gāze; a structure on the plot = building_on_plot; a plan/cadastral
   map = zoning.

## FIELDS
- communications: subset of ["elektrība","ūdens","kanalizācija","gāze","siltums"]; [] if none.
- zoning: the functional-zone CODE if identifiable (DzS, DzS1, DzM, DzM1, DzD, DzD1, JC,
  JC2, JC4, JC8, P, R, TR, TA, DA, M, L, Ū), or the closest code if described in words.
  Return ONLY the code (e.g. "JC8"). Do NOT write an explanation. Else "Nav minēts".
- building_on_plot: one of ["nav","ir_eka","ir_pamati","nezināms"].
- building_area_m2: building floor area in m² (digits only) if a building exists AND its
  area is stated; else "Nav minēts".
- access: short access/road note; else "Nav minēts".
- extra_info: ONE short clause with the single most valuable concrete extra fact. No
  marketing. Else "Nav minēts".
- listing_intent: one of ["pārdod","iznomā","pērk_meklē","nezināms"].
- Confidence: "0.00"-"1.00".
- Debug_status: one of ["ok","wanted_ad","low_evidence","agent_detected"]. Use "wanted_ad"
  whenever listing_intent = "pērk_meklē".
- Debug_note: one short internal note.

## OUTPUT
Return ONLY the JSON object matching the schema.
"""

LAND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "communications": {
            "type": "array",
            "items": {"type": "string", "enum": ["elektrība", "ūdens", "kanalizācija", "gāze", "siltums"]},
        },
        "zoning": {"type": "string"},
        "building_on_plot": {"type": "string", "enum": ["nav", "ir_eka", "ir_pamati", "nezināms"]},
        "building_area_m2": {"type": "string"},
        "access": {"type": "string"},
        "extra_info": {"type": "string"},
        "listing_intent": {"type": "string", "enum": ["pārdod", "iznomā", "pērk_meklē", "nezināms"]},
        "Confidence": {"type": "string"},
        "Debug_status": {"type": "string", "enum": ["ok", "wanted_ad", "low_evidence", "agent_detected"]},
        "Debug_note": {"type": "string"},
    },
    "required": [
        "communications", "zoning", "building_on_plot", "building_area_m2", "access",
        "extra_info", "listing_intent", "Confidence", "Debug_status", "Debug_note",
    ],
}

ZONING_TEMPLATES = {
    "DzS": "Zemes gabalam ir savrupmāju dzīvojamās apbūves zonējums ar kodu (DzS), kas nozīmē, ka tas ir paredzēts privātmāju un individuālo dzīvojamo ēku būvniecībai. Komercdarbība šajā zonā ir ļoti ierobežota.",
    "DzM": "Zemes gabalam ir mazstāvu dzīvojamās apbūves zonējums ar kodu (DzM), kas nozīmē, ka tajā var būvēt rindu mājas un nelielas mazstāvu daudzdzīvokļu ēkas.",
    "DzD": "Zemes gabalam ir daudzdzīvokļu dzīvojamās apbūves zonējums ar kodu (DzD), kas nozīmē, ka tajā var būvēt daudzdzīvokļu mājas, un ēku pirmajos stāvos var izvietot arī veikalus, birojus un pakalpojumus.",
    "JC": "Zemes gabalam ir jauktas centra apbūves zonējums, kas piemērots biroju, tirdzniecības, pakalpojumu, dzīvojamās apbūves, kā arī noliktavu vai vieglās ražošanas funkciju attīstībai.",
    "JC2": "Zemes gabalam ir jauktas centra apbūves zonējums, kas piemērots biroju, tirdzniecības, pakalpojumu, dzīvojamās apbūves, kā arī noliktavu, vairumtirdzniecības vai vieglās ražošanas funkciju attīstībai.",
    "JC4": "Zemes gabalam ir jauktas centra apbūves zonējums, kas piemērots biroju, tirdzniecības, pakalpojumu, dzīvojamās apbūves, noliktavu, vairumtirdzniecības un vieglās ražošanas funkciju attīstībai.",
    "JC8": "Zemes gabalam ir jauktas centra apbūves zonējums ar kodu (JC8) — funkcionālā zona Rīgas vēsturiskā centra un tā aizsardzības zonas teritorijā ar plašu jauktas izmantošanas spektru: dzīvojamā apbūve (savrupmājas, rindu un daudzdzīvokļu mājas), biroji, tirdzniecība un pakalpojumi, tūrisma un atpūtas, kultūras, sporta, izglītības, veselības un sociālās aprūpes iestādes; papildus atļauta arī vieglā rūpniecība un transporta infrastruktūra.",
    "P": "Zemes gabalam ir publiskās apbūves zonējums ar kodu (P), kas nozīmē, ka tas ir paredzēts birojiem, izglītības, medicīnas, sporta un pakalpojumu objektiem.",
    "R": "Zemes gabalam ir ražošanas (industriālās apbūves) zonējums ar kodu (R), kas nozīmē, ka tas ir paredzēts noliktavām, ražošanas uzņēmumiem un loģistikas objektiem.",
    "TR": "Zemes gabalam ir transporta infrastruktūras zonējums ar kodu (TR), kas nozīmē, ka tas ir paredzēts ceļiem, transporta koridoriem, stāvvietām un transporta apkalpes objektiem.",
    "TA": "Zemes gabalam ir tehniskās infrastruktūras zonējums ar kodu (TA), kas nozīmē, ka tas ir paredzēts inženiertīkliem — elektroapgādei, ūdensapgādei, kanalizācijai, sakariem un atkritumu apsaimniekošanai.",
    "DA": "Zemes gabalam ir dabas un apstādījumu teritorijas zonējums ar kodu (DA), kas nozīmē, ka tas ir parks vai zaļā zona, un apbūves potenciāls tajā parasti ir zems.",
    "M": "Zemes gabalam ir meža teritorijas zonējums ar kodu (M), kas nozīmē, ka apbūve tajā ir ļoti specifiska un stingri ierobežota.",
    "L": "Zemes gabalam ir lauksaimniecības teritorijas zonējums ar kodu (L), kas nozīmē, ka tas ir paredzēts lauksaimnieciskajai ražošanai un viensētām, reizēm arī tūrisma vai servisa objektiem.",
    "Ū": "Zemes gabalam ir ūdens teritorijas zonējums ar kodu (Ū), kas nozīmē, ka tas ir paredzēts piestātnēm, ūdens izmantošanai un specifiskiem ar ūdeni saistītiem objektiem.",
}

# Kolonnas, ko zemes AI raksta (agent_ai_poller update; agent_locked respektēts).
LAND_OUTPUT_FIELDS = [
    "communications", "zoning", "building_on_plot", "building_area_m2", "access",
    "extra_info", "listing_intent", "land_description", "Confidence",
]

_LAND_UNKNOWNS = {"", "nav minēts", "nav minets", "nezināms", "nezinams", "unknown", "none"}


def is_land_row(row: Dict[str, Any]) -> bool:
    grp = str(row.get("group") or "").strip().lower()
    ltype = str(row.get("listing_type") or "").strip().lower()
    sg = str(row.get("Space_group") or "").strip().lower()
    return grp == "zeme" or ltype == "plot" or sg == "zeme"


def zoning_sentence(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    c = str(code).strip()
    if c.lower() in _LAND_UNKNOWNS:
        return None
    if c in ZONING_TEMPLATES:
        return ZONING_TEMPLATES[c]
    m = re.match(r"^(DzS|DzM|DzD)\d*$", c, re.IGNORECASE)
    if m:
        base = {"dzs": "DzS", "dzm": "DzM", "dzd": "DzD"}[m.group(1).lower()]
        return ZONING_TEMPLATES.get(base)
    if re.match(r"^JC\d*$", c, re.IGNORECASE):
        return ZONING_TEMPLATES["JC"]
    return ZONING_TEMPLATES.get(re.sub(r"\d+$", "", c))


def _clean(v: Any) -> str:
    s = str(v or "").strip()
    return "" if s.lower() in _LAND_UNKNOWNS else s


def build_land_description(row_data: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Saliek zemes aprakstu no fiksētā šablona. BEZ adreses (tā ir titulā)."""
    pt = str(row_data.get("price_type") or "").strip().lower()
    deal = "iznomāts" if pt in ("monthly", "mēneša") else "pārdots"

    area_raw = _clean(row_data.get("Zemes_gabals_m2")) or _clean(row_data.get("area_m2"))
    area = re.sub(r"[^0-9]", "", area_raw)
    area_txt = f" {area} m²" if area else ""

    parts = [f"Tiek {deal} zemes gabals{area_txt}."]

    lu = _clean(row_data.get("land_use"))
    if lu:
        parts.append(f" Pielietojums — {lu[0].lower() + lu[1:]}.")

    comms = [c for c in (result.get("communications") or []) if c]
    if comms:
        core = ["elektrība", "ūdens", "kanalizācija", "gāze"]
        if all(c in comms for c in core):
            parts.append(" Zemes gabalam pieejamas visas komunikācijas (elektrība, ūdens, kanalizācija, gāze).")
        else:
            lst = comms[0] if len(comms) == 1 else ", ".join(comms[:-1]) + " un " + comms[-1]
            parts.append(f" Zemes gabalam pieejama {lst}.")

    zs = zoning_sentence(result.get("zoning"))
    if zs:
        parts.append(" " + zs)

    if str(result.get("building_on_plot") or "").strip().lower() == "ir_eka":
        ba = re.sub(r"[^0-9]", "", _clean(result.get("building_area_m2")))
        parts.append(f" Uz gabala ir ēka (~{ba} m²)." if ba else " Uz gabala ir ēka.")

    extra = _clean(result.get("extra_info"))
    if extra:
        parts.append(f" {extra.rstrip('.')}.")

    return "".join(parts)


def _build_land_messages(url: str, text: str, image_urls: List[str]) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [
        {"type": "input_text", "text": (
            "Tu analizē ZEMES (zemes gabala) sludinājumu. Izlasi VISU tekstu un izskati "
            "VISAS bildes — svarīgais (zonējuma kods, komunikācijas, ēka, plāns) bieži ir "
            "zemāk tekstā vai bildēs. Drīksti atbildēt tikai ar JSON pēc schema."
        )},
        {"type": "input_text", "text": f"Sludinājuma atsauce: {url}"},
        {"type": "input_text", "text": f"Sludinājuma teksts:\n{text}"},
    ]
    for i, u in enumerate(image_urls, start=1):
        content.append({"type": "input_text", "text": f"#photo-{i}"})
        content.append({"type": "input_image", "image_url": u, "detail": "high"})
    content.append({"type": "input_text", "text": LAND_PROMPT})
    return [{"role": "user", "content": content}]


def analyze_land(client, model: str, url: str, text: str, image_urls: List[str]) -> Dict[str, Any]:
    """Izsauc OpenAI ar zemes promptu+shēmu. Zemei bildes NAV obligātas (teksts galvenais)."""
    response = client.responses.create(
        model=model,
        input=_build_land_messages(url, text, image_urls or []),
        text={"format": {"type": "json_schema", "name": "land_schema", "schema": LAND_SCHEMA, "strict": True}},
    )
    import json
    data = json.loads(response.output_text)
    if not data.get("Debug_status"):
        data["Debug_status"] = "ok"
    return data
