"""AI sludinājuma teksta ģenerators (OpenAI gpt-5.4-mini).

Aizvieto slot-based render_body ar AI-ģenerētu, gramatiski pareizu,
plūstošu tekstu pēc Raimonda 3-daļu struktūras (2026-05-17 lēmums,
memory melna-kaste-templates-slot-based). Fallback uz wp_templates ja
OpenAI kļūda — publish NEKAD nefail teksta dēļ.

Lieto to pašu OpenAI klientu kā ai_parse.py (Responses API, free tier).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
import wp_templates  # fallback + struktūras avots

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

# Lauki, ko padodam modelim (tikai jēgpilnie AI-analizētie)
_FIELDS = [
    "Space_group", "Potential_space_group", "street", "district", "city",
    "area_m2", "floor", "Space_condition", "Logu_type", "Griestu_augstums",
    "Cik_telpas", "cik_WC", "Gridas_materials", "Gridas_izturiba_kg_m2",
    "Dalama_telpa", "Mebeleta_telpa", "Sava_ieeja_check",
    "Ir_izlietne_telpa_check", "Virtuve_check", "Balkons_check",
    "building_type", "building_class", "Building_description", "Apkure",
    "Ventilacijas_sistema_check", "electric_power_kw", "Parkings",
    "Rampa_logistikai_check", "Pacelamie_varti_check", "Auto_pacelajs_check",
    "Apsargajama_teritorija_check", "Nozogota_teritorija_check",
    "Sava_eka_check", "price", "price_type", "price_per_m2",
    "Apsaimniekosanas_maksa", "Komunalie", "Papildu_maksas", "Agent_comment",
]

_MISSING = {"", "unknown", "nezināms", "nav", "none", "null", "n/a", "-",
            "~", "unkown", "not checked"}

_SYSTEM = (
    "Tu esi RG Commerce komercīpašumu mākleris, kas raksta latviešu valodā. "
    "Raksti gramatiski nevainojamu, plūstošu, profesionālu un viegli lasāmu "
    "sludinājuma aprakstu. Izmanto TIKAI dotos faktus — NEKO neizdomā un "
    "nepievieno faktus, kuru nav. Ja kāda lauka nav, vienkārši izlaid to. "
    "Saglabā mēru — bez pārspīlētiem superlatīviem.\n"
    "STINGRI AIZLIEGTS pievienot ēkai apzīmējumus, kuru NAV dotajos faktos: "
    "vecuma minējumi ('vecāka', 'vecā', 'jauna ēka'), atrašanās/kategorijas vārdi "
    "('pilsētas', 'centra'), vai labuma apgalvojumi ('nodrošina klientu plūsmu', "
    "'laba redzamība'). building_type lieto PRECĪZI tā, kā dots (piem. 'Jaukta tipa "
    "ēka') — to NEpārfrāzē un NEpapildina ar saviem īpašības vārdiem. "
    "Building_description lieto, kā dots, neizgrezno."
)

_FORMAT = """Izveido aprakstu PRECĪZI šādā 3-daļu struktūrā (HTML):

<p><strong>Pieejamas nomai {veids} ar kopējo platību {platība} m² {rajons} rajonā.</strong></p>
<p>Telpas atrodas {stāvs}. stāvā.</p>
<p><strong>Par telpām:</strong></p>
<p>{plūstošs apraksts par pašu telpu — stāvoklis, logi, griestu augstums, plānojums (telpu/sanmezglu skaits), grīdas, vai dalāma, mēbelēta, ieeja, izlietne, virtuve, balkons, kam piemērota}</p>
<p><strong>Par ēku:</strong></p>
<p>{plūstošs apraksts par ēku — tips, klase, ēkas apraksts, apkure, ventilācija, elektrojauda, drošība/rampa/vārti, autostāvvieta}</p>
<p><strong>Izmaksas:</strong></p>
<p><strong>Noma mēnesī: {cena} EUR.</strong> <strong>Cena par m²: {m2cena} EUR/m².</strong> (un apsaimniekošana/komunālie, ja ir)</p>
<p>Sazinieties ar mums, lai uzzinātu vairāk vai vienotos par apskati. 🏢</p>

Noteikumi:
- Ja īpašums tiek PĀRDOTS (price_type regular/parastā/sale), lieto "Pārdošanā" un "Cena:" nevis "Noma mēnesī:".
- Daļu "Par telpām:" un "Par ēku:" raksti kā 1-3 dabiskus teikumus (NE sausu sarakstu ar semikoliem).
- Ja stāvs nezināms — izlaid otro rindu. Stāva rinda ir TIKAI "Telpas atrodas {stāvs}. stāvā." — NEKO nepievieno (ne "kas nodrošina...", ne par redzamību/klientu plūsmu).
- Ja m² cena nav dota, aprēķini: cena / platība (noapaļo līdz 2 cipariem).
- Atgriez TIKAI HTML (<p>, <strong>), bez papildu komentāriem, bez markdown.
"""


def _facts(raw: dict) -> str:
    lines = []
    for k in _FIELDS:
        v = raw.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in _MISSING:
            continue
        lines.append(f"- {k}: {s}")
    return "\n".join(lines)


def generate_body(space_group: str, raw: dict) -> str:
    """AI-ģenerēts HTML apraksts; fallback uz slot-šablonu ja kļūda."""
    if not OPENAI_API_KEY:
        return wp_templates.render_body(space_group, raw)
    try:
        from openai import OpenAI
        _verify = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) \
            not in ("0", "false", "False")
        if not _verify:
            import httpx
            client = OpenAI(api_key=OPENAI_API_KEY,
                            http_client=httpx.Client(verify=False))
        else:
            client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = (
            f"{_FORMAT}\n\nĪPAŠUMA DATI (lieto tikai šos):\n{_facts(raw)}\n\n"
            f"Telpu veids (Space_group): {space_group}"
        )
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system",
                 "content": [{"type": "input_text", "text": _SYSTEM}]},
                {"role": "user",
                 "content": [{"type": "input_text", "text": prompt}]},
            ],
        )
        html = (resp.output_text or "").strip()
        # Notīra iespējamo markdown koda bloku
        if html.startswith("```"):
            html = html.split("```")[1]
            if html.lstrip().lower().startswith("html"):
                html = html.lstrip()[4:]
        html = html.strip()
        if "<p>" not in html or len(html) < 60:
            return wp_templates.render_body(space_group, raw)
        return html
    except Exception as e:
        print(f"  ! AI teksts neizdevās ({str(e)[:120]}), lieto slot-šablonu")
        return wp_templates.render_body(space_group, raw)


if __name__ == "__main__":
    demo = {
        "Space_group": "Tirdzniecība", "district": "Āgenskalns", "city": "Rīga",
        "street": "Kalnciema 40", "area_m2": "125", "floor": "1",
        "Space_condition": "Labs", "Logu_type": "Lielie Logi",
        "Griestu_augstums": "3.5", "Cik_telpas": "2", "cik_WC": "1",
        "Gridas_materials": "Betona grīda", "Gridas_izturiba_kg_m2": "300",
        "Dalama_telpa": "Jā", "Sava_ieeja_check": "checked",
        "Ir_izlietne_telpa_check": "checked", "building_type": "Jaukta tipa ēka",
        "building_class": "B",
        "Building_description": "3 stāvu fasādes ēka ar atsevišķu ielas ieeju, "
        "vitrīnas tipa logiem pirmajā stāvā un vairākiem nomniekiem ēkā.",
        "Apkure": "Centrālā", "Ventilacijas_sistema_check": "checked",
        "Parkings": "Tikai ielas parking", "price": "700",
        "price_type": "mēneša", "Potential_space_group": "Birojs, Studija",
    }
    print(generate_body("Tirdzniecība", demo))
