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
- asset_summary — the MAIN descriptive narrative in LATVIAN: 1-2 cohesive paragraphs
  (~3-6 sentences) that BLEND together: (a) the property composition (the buildings,
  their functions/potential — given in context), (b) the investment framing, (c) your
  image-based quality/condition observations, AND (d) ALL concrete facts the agent
  provided in their notes (context "Aģenta apraksts" / "Ēkas īpašumā" / financials).
  It must read as ONE flowing description, not a list.
  * PRESERVE every concrete agent-provided fact (piem. atsevišķa elektroapakšstacija,
    piebraukšana, divējāda izmantošana, komunikācijas). Do NOT drop or contradict them.
  * If INCOME_PRESENT = yes → weave in stabilu naudas plūsmu / nomniekus / atdevi
    (do NOT state the exact number — added separately as a fact line).
  * If INCOME_PRESENT = no → weave in POTENCIĀLU: vairākas ēkas komercdarbībai,
    tūlītēju izmantošanu, attīstības/pārprofilēšanas iespējas.
  Do NOT invent facts not in text/agent notes/images. No marketing clichés. This is
  the paragraph that goes UNDER the numbered building list — make it feel connected.
- highlights — [] (nelietojam; atstāj tukšu).

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


def context_text(row_data: Dict[str, Any]) -> str:
    """Strukturētais anketas konteksts investīciju AI ievadei — lai naratīvs
    saplūst ar ēkām/skaitļiem/zemi (ne izolēts). Pievieno bāzes tekstam."""
    lines: List[str] = []
    blds = row_data.get("buildings") or []
    if isinstance(blds, str):
        try:
            blds = json.loads(blds)
        except Exception:
            blds = []
    b_lines = []
    for b in (blds or []):
        if isinstance(b, dict) and _clean(b.get("type")):
            a = _num(b.get("area_m2"))
            note = _clean(b.get("note"))
            b_lines.append(f"- {b['type']}" + (f" {a} m²" if a else "") + (f" ({note})" if note else ""))
    if b_lines:
        lines.append("Ēkas īpašumā (aģents ievadījis):\n" + "\n".join(b_lines))
    zg = _num(row_data.get("Zemes_gabals_m2"))
    if zg:
        lines.append(f"Zemes platība: {zg} m²")
    inc = _clean(row_data.get("investment_income"))
    if inc:
        lines.append(f"Īres ienākumi (aģents): {inc}")
    yl = _clean(row_data.get("investment_yield"))
    if yl:
        lines.append(f"Atdeve (aģents): {yl}")
    return "\n\n".join(lines)


def build_investment_description(row_data: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Saliek investīciju objekta tekstu. BEZ virsraksta (WP tituls = adrese).
    Bloki atdalīti ar '\\n\\n' (render_body → atsevišķas rindkopas); ēku saraksts
    ar '\\n' (katra ēka sava rindiņā). Identiskas ēkas apvieno ar skaitu."""
    pt = str(row_data.get("price_type") or "").strip().lower()
    deal = "iznomāts" if pt in ("monthly", "mēneša") else "pārdots"

    area = _num(row_data.get("area_m2"))
    area_txt = f" ar kopējo platību {area} m²" if area else ""

    # Ēku saraksts ar dedup (identiskas tips+platība+piezīme → viena rinda + skaits).
    blds = row_data.get("buildings") or []
    if isinstance(blds, str):
        try:
            blds = json.loads(blds)
        except Exception:
            blds = []
    grouped: List[Dict[str, Any]] = []  # [{key:(t,a,note), count}]
    for b in (blds or []):
        if not isinstance(b, dict):
            continue
        t = _clean(b.get("type"))
        if not t:
            continue
        key = (t, _num(b.get("area_m2")), _clean(b.get("note")))
        hit = next((g for g in grouped if g["key"] == key), None)
        if hit:
            hit["count"] += 1
        else:
            grouped.append({"key": key, "count": 1})

    _WORDS = {2: "divas", 3: "trīs", 4: "četras", 5: "piecas", 6: "sešas"}
    list_lines: List[str] = []
    for g in grouped:
        t, a, note = g["key"]
        line = t + (f" – {a} m²" if a else "")
        extras: List[str] = []
        if note:
            extras.append(note)
        if g["count"] > 1:
            cnt = _WORDS.get(g["count"], str(g["count"]))
            extras.append(f"šādas ēkas ir {cnt}")
        if extras:
            line += " (" + "; ".join(extras) + ")"
        list_lines.append(line)

    blocks: List[str] = []
    intro = f"Tiek {deal} investīciju objekts{area_txt}."
    if list_lines:
        blocks.append(intro + "\nĪpašumā ietilpst:")
        n = len(list_lines)
        numbered = [f"{i}) {ln}{'.' if i == n else ';'}" for i, ln in enumerate(list_lines, 1)]
        blocks.append("\n".join(numbered))
    else:
        blocks.append(intro)

    # Zemes platība (ja ievadīta) — sava rindkopa.
    zg = _num(row_data.get("Zemes_gabals_m2"))
    if zg:
        blocks.append(f"Zemes platība: {zg} m².")

    # Finanšu skaitļi — aģenta ievadītie, verbatim (sava rindkopa).
    income = _clean(row_data.get("investment_income"))
    yld = _clean(row_data.get("investment_yield"))
    if income:
        blocks.append(f"Īres ienākumi: {income}; atdeve: {yld}." if yld else f"Īres ienākumi: {income}.")
    elif yld:
        blocks.append(f"Prognozētā atdeve: {yld}.")

    # AI investīciju naratīvs (iepin ēkas+skaitļus+aģenta faktus). Fallback uz
    # aģenta aprakstu, ja AI neizdevās (lai teksts nepaliek bez prozas).
    summary = _clean(result.get("asset_summary"))
    if not summary:
        summary = _clean(row_data.get("Agent_comment"))
    if summary:
        blocks.append(summary)

    return "\n\n".join(blocks)


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
