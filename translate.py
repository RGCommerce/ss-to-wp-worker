"""Mājaslapas satura tulkošana (LV → EN/RU) — kopīgais modulis.

Princips (Raimonds 2026-09-05, sk. panelis HANDOFF_DAUDZVALODU.md):
AI iztulko VIENREIZ un saglabā DB (properties.content_translations).
Tulko pa RINDKOPĀM (dabīga valoda), nevis pa vārdiem; atkārtoti tulko
TIKAI mainītās rindkopas (src_hash salīdzinājums) — cost-efficient:
piem. apsaimniekotāja edits, kas maina 1 rindkopu 20 sludinājumos,
pārtulko tikai to 1 rindkopu katram, ne visu tekstu. Vienādas rindkopas
(piem. tas pats ēkas apraksts daudzos sludinājumos) tulkojas VIENREIZ —
globālais kešs pēc src_hash.

Lietošana:
    from translate import translate_field, get_translation
    translate_field("listing", "12345", "description", lv_html)   # pēc LV teksta izveides
    en = get_translation("listing", "12345", "description", "en") # rādīšanai (None → fallback LV)

CLI (tests / viens ieraksts):
    python translate.py <listing_id>            # iztulko viena listinga aprakstu EN+RU
    python translate.py <listing_id> --show en  # parāda saglabāto tulkojumu
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# Tulkošanai pietiek ar mini modeli — lēts un labs valodām
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "gpt-4o-mini")

LANGS = ("en", "ru")
_LANG_NAME = {"en": "English", "ru": "Russian"}

_DDL = """
CREATE TABLE IF NOT EXISTS properties.content_translations (
    id            bigserial PRIMARY KEY,
    content_type  text        NOT NULL,
    content_id    text        NOT NULL,
    field         text        NOT NULL,
    lang          text        NOT NULL,
    seq           int         NOT NULL,
    src_hash      text        NOT NULL,
    translated    text        NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (content_type, content_id, field, lang, seq)
);
CREATE INDEX IF NOT EXISTS content_translations_hash_idx
    ON properties.content_translations (src_hash, lang);
"""


def _db():
    import psycopg
    return psycopg.connect(DATABASE_URL)


_TABLE_READY = False


def ensure_table(conn=None) -> None:
    """Izveido tabulu, ja nav (aditīva — droša atkārtoti)."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    own = conn is None
    if own:
        conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        _TABLE_READY = True
    finally:
        if own:
            conn.close()


# ── Rindkopu sadalīšana ──────────────────────────────────────────────
# HTML tekstam (sludinājumi): pa <p>…</p> / <h*>…</h*> blokiem.
# Vienkāršam tekstam: pa tukšām rindām. Rindkopa = tulkošanas vienība.

_HTML_BLOCK = re.compile(r"<(p|h[1-6]|li|div)\b[^>]*>.*?</\1>", re.S | re.I)


def split_paragraphs(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if "<p" in text.lower() or "<h" in text.lower() or "<li" in text.lower():
        blocks = [m.group(0).strip() for m in _HTML_BLOCK.finditer(text)]
        if blocks:
            return blocks
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _hash(par: str) -> str:
    # Normalizē atstarpes, lai kosmētiskas izmaiņas neizraisa pārtulkošanu
    norm = re.sub(r"\s+", " ", par.strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# ── Tulkošana ar OpenAI ──────────────────────────────────────────────

_TR_SYSTEM = (
    "You are a professional real-estate translator. Translate the given "
    "Latvian text into {lang}. Rules:\n"
    "- Translate NATURALLY, the way a native-speaking commercial real estate "
    "agent would write it — not word-for-word. Rephrase where the target "
    "language reads better, but keep every fact exactly.\n"
    "- NEVER add, remove or change facts, numbers, addresses or names.\n"
    "- Keep ALL HTML tags and structure exactly as in the source.\n"
    "- Keep Latvian proper names (street names, districts, company names) "
    "as they are (do not translate street names; 'iela' may stay).\n"
    "- Keep units (m², €, kW) unchanged.\n"
    "- Output ONLY the translation, no explanations, no quotes."
)


def _openai_client():
    from openai import OpenAI
    verify = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) not in (
        "0", "false", "False")
    if not verify:
        import httpx
        return OpenAI(api_key=OPENAI_API_KEY, http_client=httpx.Client(verify=False))
    return OpenAI(api_key=OPENAI_API_KEY)


def _translate_one(par: str, lang: str) -> str | None:
    """Iztulko vienu rindkopu. None ja neizdodas (tad paliek bez — fallback LV)."""
    if not OPENAI_API_KEY:
        return None
    try:
        client = _openai_client()
        resp = client.responses.create(
            model=TRANSLATE_MODEL,
            input=[
                {"role": "system", "content": [{
                    "type": "input_text",
                    "text": _TR_SYSTEM.format(lang=_LANG_NAME[lang])}]},
                {"role": "user", "content": [{
                    "type": "input_text", "text": par}]},
            ],
        )
        out = (resp.output_text or "").strip()
        return out or None
    except Exception as e:
        print(f"  ! tulkojums ({lang}) neizdevās: {str(e)[:100]}")
        return None


# ── Galvenā API ──────────────────────────────────────────────────────

def translate_field(content_type: str, content_id: str, field: str,
                    lv_text: str, langs=LANGS, conn=None) -> dict:
    """Nodrošina, ka lauka tulkojumi EN/RU ir aktuāli.

    Tulko TIKAI rindkopas, kuru src_hash mainījies vai kuru vēl nav.
    Nemainītās atstāj. Vienādām rindkopām (jebkurā saturā) lieto globālo
    kešu pēc src_hash. Liekās seq rindas izdzēš. Atgriež statistiku.
    """
    stats = {"paragraphs": 0, "translated": 0, "cached": 0, "kept": 0, "failed": 0}
    pars = split_paragraphs(lv_text)
    stats["paragraphs"] = len(pars)
    own = conn is None
    if own:
        conn = _db()
    try:
        ensure_table(conn)
        cid = str(content_id)
        with conn.cursor() as cur:
            for lang in langs:
                # Esošie tulkojumi šim laukam
                cur.execute(
                    "SELECT seq, src_hash FROM properties.content_translations "
                    "WHERE content_type=%s AND content_id=%s AND field=%s AND lang=%s",
                    (content_type, cid, field, lang))
                existing = {r[0]: r[1] for r in cur.fetchall()}
                for seq, par in enumerate(pars):
                    h = _hash(par)
                    if existing.get(seq) == h:
                        stats["kept"] += 1
                        continue  # nemainīta rindkopa — tulkojums paliek
                    # Globālais kešs: tāda pati LV rindkopa jau tulkota jebkur?
                    cur.execute(
                        "SELECT translated FROM properties.content_translations "
                        "WHERE src_hash=%s AND lang=%s LIMIT 1", (h, lang))
                    row = cur.fetchone()
                    if row:
                        translated = row[0]
                        stats["cached"] += 1
                    else:
                        translated = _translate_one(par, lang)
                        if translated is None:
                            stats["failed"] += 1
                            continue  # fallback = LV; rezerves darbs vēlāk mēģinās vēl
                        stats["translated"] += 1
                    cur.execute(
                        "INSERT INTO properties.content_translations "
                        "(content_type, content_id, field, lang, seq, src_hash, translated) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (content_type, content_id, field, lang, seq) "
                        "DO UPDATE SET src_hash=EXCLUDED.src_hash, "
                        "translated=EXCLUDED.translated, updated_at=now()",
                        (content_type, cid, field, lang, seq, h, translated))
                # Izdzēš liekās seq (rindkopu skaits samazinājies)
                cur.execute(
                    "DELETE FROM properties.content_translations "
                    "WHERE content_type=%s AND content_id=%s AND field=%s "
                    "AND lang=%s AND seq >= %s",
                    (content_type, cid, field, lang, len(pars)))
        conn.commit()
    finally:
        if own:
            conn.close()
    return stats


def get_translation(content_type: str, content_id: str, field: str,
                    lang: str, conn=None) -> str | None:
    """Saliek tulkoto tekstu pēc seq. None, ja tulkojuma nav (→ rādīt LV)."""
    if lang not in LANGS:
        return None
    own = conn is None
    if own:
        conn = _db()
    try:
        ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT translated FROM properties.content_translations "
                "WHERE content_type=%s AND content_id=%s AND field=%s AND lang=%s "
                "ORDER BY seq", (content_type, str(content_id), field, lang))
            rows = [r[0] for r in cur.fetchall()]
        if not rows:
            return None
        joiner = "\n" if "<p" in rows[0].lower() else "\n\n"
        return joiner.join(rows)
    finally:
        if own:
            conn.close()


# ── Sludinājuma ērtais ietinums (hook no publish plūsmas) ────────────
# PILNAIS sludinājuma teksts dzīvo WP post_content (ģenerē ai_text +
# segmenti publicēšanas brīdī) — DB Building_description ir tikai īsais
# ēkas apraksts. Tāpēc avots = WP rgc_catalog_detail (publisks, bez auth).
# content_id = wp_post_id (to pašu ID lieto mājaslapas frontends).

WP_BASE = os.getenv("WP_BASE", "https://rgcommerce.lv")


def _wp_description(wp_post_id) -> str | None:
    import httpx
    try:
        r = httpx.get(WP_BASE + "/wp-admin/admin-ajax.php",
                      params={"action": "rgc_catalog_detail", "id": int(wp_post_id)},
                      timeout=25)
        d = r.json()
        return (d.get("description") or "").strip() or None
    except Exception as e:
        print(f"  ! WP detail {wp_post_id}: {str(e)[:80]}")
        return None


def translate_listing(wp_post_id, html: str | None = None, conn=None) -> dict | None:
    """Iztulko viena listinga WP aprakstu EN+RU.

    html — ja publicēšanas plūsmai teksts jau rokā, padod to (bez WP zvana);
    citādi paņem no WP. Best-effort: kļūda NEKAD neaptur publicēšanu.
    """
    try:
        text = html or _wp_description(wp_post_id)
        if not text:
            return None
        return translate_field("listing", wp_post_id, "description", text, conn=conn)
    except Exception as e:
        print(f"  ! translate_listing({wp_post_id}) kļūda: {str(e)[:120]}")
        return None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        sys.exit(0)
    lid = args[0]
    if "--show" in args:
        lang = args[args.index("--show") + 1] if len(args) > args.index("--show") + 1 else "en"
        out = get_translation("listing", lid, "description", lang)
        print(out if out else f"(nav {lang} tulkojuma — rādītos LV)")
    else:
        print(f"Tulko listingu {lid} → EN+RU …")
        st = translate_listing(lid)
        print("Rezultāts:", st)
