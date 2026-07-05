"""investment_ai.py — INVESTĪCIJU OBJEKTA AI (prompts + teksts).

Spoguļo land_ai.py struktūru. Investīciju objekts = īpašums (bieži ar vairākām
dažāda tipa ēkām) ko pārdod kā investīciju. AI VIENMĒR iziet cauri visām bildēm,
nosaka telpu kvalitāti + investīciju stratēģiju (Core/Value-Add/Distressed) un
uzraksta investīciju-orientētu teksta kopsavilkumu. Aģents ievada TIKAI skaitļus
(īres ienākumi, atdeve) — AI tos neizdomā, bet lieto tekstā.

Divi teksta akcenti (izvēlas pēc tā, vai ir ienākumu dati):
  * IENĀKUMUS NESOŠS (agent ievadījis īres ienākumus) → uzsver stabilu naudas
    plūsmu, nomniekus, atdevi (Core/Core+ virziens).
  * ATTĪSTĀMS / bez nomniekiem (nav ienākumu) → uzsver potenciālu, vairākas ēkas
    komercdarbībai, tūlītēju izmantošanu (Value-Add/Distressed).

Lietojums (anketa, agent_ai_poller):
    import investment_ai
    if investment_ai.is_investment_row(row):
        result = investment_ai.analyze_investment(client, MODEL, url, text, image_urls, has_income)
        result["investment_description"] = investment_ai.build_investment_description(row, result)
        # → Investiciju_strategija = result["investment_strategy"]
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


INVESTMENT_PROMPT = r"""
# KOMERCIĀLA INVESTĪCIJU OBJEKTA ANALĪZE — ROBOTA REŽĪMS

## GOAL
Analyze ONE commercial INVESTMENT property using BOTH the listing text AND all
images, plus the agent-provided structured building list and financial numbers.
Judge it as an INVESTMENT asset (not as a single user premise). Output structured
fields + a short investment-angle summary in LATVIAN. You are a robot: no fluff.

## HARD RULES
0. READ everything: text, all images, the building list, and whether income data
   is provided (INCOME_PRESENT flag below).
1. Do NOT invent financial numbers. Rental income / yield come ONLY from the agent
   (given below); never guess them. You MAY reason about condition, quality,
   positioning, and potential from what you see.
2. Output MUST match the JSON schema exactly. No markdown. Latvian for text fields.
3. Never output price, phone, or agent name. Never restate the raw building list
   verbatim (it is added separately) — instead give the INVESTMENT interpretation.

## WHAT TO DETERMINE
- investment_strategy — the investor profile of the asset:
  * "Core/Core+"  = professionally positioned, good/very good condition, stable,
     little CAPEX; typically WHEN income is present and the asset is maintained.
  * "Value-Add"   = usable but with clear repositioning / modernization / lease-up
     upside; often multiple buildings to develop, or under-optimized.
  * "Distressed"  = heavily outdated / worn / technically weak / needs major work.
  Choose based on the images + text + whether it produces income.
- quality_note — ONE short factual sentence on the physical quality/condition you
  SEE (e.g. "Ēkas ir tehniski funkcionālas, ar betonētām grīdām un paceļamiem vārtiem.").
  Only visible facts, no age/location guesses.
- asset_summary — 2-3 short LATVIAN sentences framing this as an INVESTMENT.
  * If INCOME_PRESENT = yes → emphasize stabilu naudas plūsmu, esošos nomniekus,
    atdevi, gatavību ienākumam. Do NOT state the number (added separately).
  * If INCOME_PRESENT = no → emphasize POTENCIĀLU: vairākas ēkas komercdarbībai,
    tūlītēju izmantošanu, attīstības/pārprofilēšanas iespējas.
  No marketing clichés, no invented facts.
- highlights — up to 4 short LATVIAN bullet phrases with concrete investment plus-
  points visible in evidence (piem. "Iežogota teritorija", "8 m griestu augstums",
  "Atsevišķa iebraukšana smagajam transportam"). [] if none.

## FIELDS (schema)
- investment_strategy, quality_note, asset_summary, highlights,
- Confidence "0.00"-"1.00", Debug_status one of ["ok","low_evidence"], Debug_note.

## OUTPUT
Return ONLY the JSON object matching the schema.
"""

INVESTMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "investment_strategy": {
            "type": "string",
            "enum": ["Core/Core+", "Value-Add", "Distressed"],
        },
        "quality_note": {"type": "string"},
        "asset_summary": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}},
        "Confidence": {"type": "string"},
        "Debug_status": {"type": "string", "enum": ["ok", "low_evidence"]},
        "Debug_note": {"type": "string"},
    },
    "required": [
        "investment_strategy", "quality_note", "asset_summary", "highlights",
        "Confidence", "Debug_status", "Debug_note",
    ],
}

# Kolonnas, ko investīciju AI raksta (agent_ai_poller update; agent_locked respektēts).
INVESTMENT_OUTPUT_FIELDS = ["Investiciju_strategija", "investment_description", "Confidence"]

_UNKNOWNS = {"", "nav minēts", "nav minets", "nezināms", "nezinams", "unknown", "none"}


def is_investment_row(row: Dict[str, Any]) -> bool:
    sg = str(row.get("Space_group") or "").strip().lower()
    return sg == "investīciju objekts" or sg == "investiciju objekts"


def has_income(row: Dict[str, Any]) -> bool:
    return bool(str(row.get("investment_income") or "").strip())


def _clean(v: Any) -> str:
    s = str(v or "").strip()
    return "" if s.lower() in _UNKNOWNS else s


def _num(v: Any) -> str:
    return re.sub(r"[^0-9]", "", str(v or ""))


def build_investment_description(row_data: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Saliek investīciju objekta tekstu: intro + ēku saraksts + finanšu skaitļi
    (aģenta, verbatim) + AI investīciju kopsavilkums + highlights. BEZ adreses."""
    pt = str(row_data.get("price_type") or "").strip().lower()
    deal = "iznomāts" if pt in ("monthly", "mēneša") else "pārdots"

    area = _num(row_data.get("area_m2"))
    area_txt = f" ar kopējo platību {area} m²" if area else ""
    parts = [f"Tiek {deal} investīciju objekts{area_txt}."]

    # Ēku saraksts (buildings jsonb; katra tips – platība (piezīme)).
    blds = row_data.get("buildings") or []
    if isinstance(blds, str):
        try:
            blds = json.loads(blds)
        except Exception:
            blds = []
    lines: List[str] = []
    for b in (blds or []):
        if not isinstance(b, dict):
            continue
        t = _clean(b.get("type"))
        if not t:
            continue
        a = _num(b.get("area_m2"))
        note = _clean(b.get("note"))
        line = t + (f" – {a} m²" if a else "")
        if note:
            line += f" ({note})"
        lines.append(line)
    if lines:
        parts.append(" Īpašumā ietilpst: " + "; ".join(lines) + ".")

    # Finanšu skaitļi — aģenta ievadītie, verbatim.
    income = _clean(row_data.get("investment_income"))
    yld = _clean(row_data.get("investment_yield"))
    if income:
        fin = f" Īres ienākumi: {income}."
        if yld:
            fin = f" Īres ienākumi: {income}; atdeve: {yld}."
        parts.append(fin)
    elif yld:
        parts.append(f" Prognozētā atdeve: {yld}.")

    # AI investīciju kopsavilkums.
    summary = _clean(result.get("asset_summary"))
    if summary:
        parts.append(" " + summary)

    # AI kvalitātes piezīme (ja atsevišķa no summary).
    qn = _clean(result.get("quality_note"))
    if qn and qn not in summary:
        parts.append(" " + qn)

    # Highlights (īsi plusi).
    hl = [_clean(h) for h in (result.get("highlights") or []) if _clean(h)]
    if hl:
        parts.append(" Priekšrocības: " + ", ".join(hl) + ".")

    return "".join(parts)


def _build_messages(url: str, text: str, image_urls: List[str], income_present: bool) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = [
        {"type": "input_text", "text": (
            "Tu analizē KOMERCIĀLU INVESTĪCIJU OBJEKTU. Izlasi VISU tekstu, izskati "
            "VISAS bildes un ēku sarakstu. Vērtē to kā investīciju aktīvu. "
            f"INCOME_PRESENT = {'yes' if income_present else 'no'}. "
            "Atbildi tikai ar JSON pēc schema (teksta lauki latviski)."
        )},
        {"type": "input_text", "text": f"Sludinājuma atsauce: {url}"},
        {"type": "input_text", "text": f"Sludinājuma teksts un dati:\n{text}"},
    ]
    for i, u in enumerate(image_urls, start=1):
        content.append({"type": "input_text", "text": f"#photo-{i}"})
        content.append({"type": "input_image", "image_url": u, "detail": "high"})
    content.append({"type": "input_text", "text": INVESTMENT_PROMPT})
    return [{"role": "user", "content": content}]


def analyze_investment(client, model: str, url: str, text: str,
                       image_urls: List[str], income_present: bool = False) -> Dict[str, Any]:
    """Izsauc OpenAI ar investīciju promptu+shēmu. Bildes svarīgas kvalitātes vērtējumam."""
    response = client.responses.create(
        model=model,
        input=_build_messages(url, text, image_urls or [], income_present),
        text={"format": {"type": "json_schema", "name": "investment_schema",
                          "schema": INVESTMENT_SCHEMA, "strict": True}},
    )
    data = json.loads(response.output_text)
    if not data.get("Debug_status"):
        data["Debug_status"] = "ok"
    return data
