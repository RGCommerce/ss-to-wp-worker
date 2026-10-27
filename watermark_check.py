"""watermark_check.py — ss.com ūdenszīmes drošības pārbaude (gpt-4o-mini vision).

KĀPĒC: uz WP nedrīkst nonākt bildes ar ss.com ūdenszīmi. Tās var ienākt pa
vairākiem ceļiem:
  1. Seedream (image_pipeline) reizēm NEnoņem ūdenszīmi (ģeneratīvs modelis —
     dažkārt atzīmē to kā "daļu no bildes" un uzzīmē no jauna).
  2. Aģenta anketas bildes iet uz ai_ready BEZ Seedream (agent_publish
     _copy_images) — ja aģents augšupielādē no ss.lv saglabātas bildes vai
     dublē esošu ss.lv listingu (agent_api duplicate-listing kopēja raw!),
     ūdenszīme aiziet dzīvē (piem., #109581).

Šis modulis ir "pēdējais sargs": lēta vision pārbaude (~$0.0001/bilde,
gpt-4o-mini detail=low), KEŠOTA uz diska pie listinga
(`_wm_check.json`, atslēga = filename + faila izmērs → pēc bildes
pārrakstīšanas pārbauda no jauna automātiski).

API:
    from watermark_check import check_files, has_watermark_bytes
    verdicts = check_files(listing_dir, [Path(...), ...])
    # verdicts = {"img_002.jpg": True|False|None}  (None = pārbaude neizdevās)

CLI:
    python watermark_check.py --listing 109581            # pārbauda ai_ready
    python watermark_check.py --files a.jpg,b.jpg         # brīvi faili (bez keša)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
# FRESH EYES / publicēšanas vārts partneru bildēm: stiprāks modelis (mini «high» palaida garām blāvu «EFTEN CAPITAL»
# spoku uz raiba fona — Domina 2026-10-27). ~1–2 ¢/pārbaude.
OPENAI_VISION_MODEL_STRICT = os.getenv("OPENAI_VISION_MODEL_STRICT", "gpt-4o")
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "./storage"))

_VERIFY = os.getenv("VERIFY_SSL", os.getenv("WP_VERIFY_SSL", "1")) \
    not in ("0", "false", "False")

CACHE_FILENAME = "_wm_check.json"

WM_PROMPT = (
    "You are checking a real-estate photo for the Latvian classifieds site "
    "watermark. The ss.com / ss.lv watermark is large semi-transparent "
    "white/grey text reading \"ss.com\" (often with the small line "
    "\"sludinajumu serviss\" under it) or \"ss.lv\", usually in the TOP-LEFT "
    "corner and/or a fainter copy in the BOTTOM-RIGHT corner of the photo.\n"
    "Return ONLY a JSON object with EXACTLY these fields:\n"
    "- \"watermark\": true if the ss.com/ss.lv watermark text is visible "
    "anywhere in the image (even partially or faintly), false otherwise.\n"
    "- \"where\": short note where it was seen (e.g. \"top-left\"), or \"\".\n"
    "Other overlays (agency logos, phone numbers, EXIF stamps) do NOT count — "
    "only the ss.com / ss.lv watermark. Return ONLY valid JSON."
)

# 2026-10-25 (Raimonds, MBC / Rent in Riga): ss.lv FOTO pēc Seedream pārbauda plašāk — skaitās ARĪ citu
# aģentūru ūdenszīmes, logo, «Autortiesības pieder …», tālruņi. Lieto TIKAI image_pipeline.process_bytes_checked
# (ss.lv bildes, kas tāpat iet caur Seedream), NE publicēšanas vārtā (plāni/aģenta bildes ar logo paliek kā ir).
WM_PROMPT_ANY_AGENCY = (
    "You are checking a real-estate photo for watermarks overlaid on it. "
    "The Latvian classifieds site watermark ss.com / ss.lv is large semi-transparent "
    "white/grey text reading \"ss.com\" (often with the small line "
    "\"sludinajumu serviss\" under it) or \"ss.lv\", usually in the TOP-LEFT "
    "corner and/or a fainter copy in the BOTTOM-RIGHT corner of the photo. "
    "Other real-estate agencies also overlay their own watermarks: semi-transparent logos, "
    "agency names, copyright notices such as 'Autortiesibas pieder ...', phone numbers "
    "or website addresses.\n"
    "Return ONLY a JSON object with EXACTLY these fields:\n"
    "- \"watermark\": true if ANY such overlaid watermark/logo/copyright/phone text is visible "
    "anywhere in the image (even partially, faintly, or as a ghost/remnant after editing), false otherwise.\n"
    "- \"where\": short note where it was seen (e.g. \"top-left\", \"center\"), or \"\".\n"
    "- \"text\": the overlay text/brand you can read (e.g. \"EfTEN Capital\"), or \"\".\n"
    "Real-world signage, house numbers, shop signs or documents physically present in the scene "
    "do NOT count — only overlays added on top of the photo. Return ONLY valid JSON."
)


# ---------------------------------------------------------------------------
# Vision call
# ---------------------------------------------------------------------------

def _hint_text(hint: dict | None) -> str:
    if not hint:
        return ""
    t, w = (hint.get("text") or "").strip(), (hint.get("where") or "").strip()
    if not t and not w:
        return ""
    return ("\nIMPORTANT CONTEXT: before editing, this photo HAD an overlaid watermark"
            + (f" reading \"{t}\"" if t else "") + (f" located at: {w}" if w else "")
            + ". Inspect that area with extreme care for ANY faint ghost, outline, blur halo or partial letters of it. "
              "If you can still make out any trace of it, answer watermark=true.")


def _crop_zoom(image_bytes: bytes, where: str) -> bytes | None:
    """Tuvinājums vietai («center», «top-left», …) — blāvs spoks uz raiba fona pilnā bildē paslīd garām."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        W, H = im.size
        w = (where or "center").lower()
        fx0, fx1, fy0, fy1 = 0.2, 0.8, 0.2, 0.8
        if "left" in w:
            fx0, fx1 = 0.0, 0.55
        if "right" in w:
            fx0, fx1 = 0.45, 1.0
        if "top" in w or "upper" in w:
            fy0, fy1 = 0.0, 0.55
        if "bottom" in w or "lower" in w:
            fy0, fy1 = 0.45, 1.0
        crop = im.crop((int(W * fx0), int(H * fy0), int(W * fx1), int(H * fy1)))
        scale = min(2.0, 1600 / max(crop.size))
        if scale > 1:
            crop = crop.resize((int(crop.size[0] * scale), int(crop.size[1] * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return None


def fresh_eyes(image_bytes: bytes, hint: dict | None = None) -> dict | None:
    """Pēc tīrīšanas — 3 balsis ar stingro modeli: (1) pilnā bilde AR norādi, (2) tuvinājums uz vietu AR norādi,
    (3) tuvinājums BEZ norādes. Ūdenszīme = ≥2 balsis. Tests 2026-10-27: paslīdējusī Domina (blāvs spoks) T,T,F → ir;
    tīrā Eften T,F,F → nav (viena pārbaude ar norādi mēdz pārspīlēt, bez norādes — nepamanīt)."""
    crop = _crop_zoom(image_bytes, (hint or {}).get("where") or "center")
    votes: list[dict] = []
    v1 = locate_watermark(image_bytes, any_agency=True, hint=hint, strict=True)
    if v1 is None:
        return None
    votes.append(v1)
    if crop:
        v2 = locate_watermark(crop, any_agency=True, hint=hint, strict=True)
        v3 = locate_watermark(crop, any_agency=True, hint=None, strict=True)
        votes += [v for v in (v2, v3) if v is not None]
    yes = [v for v in votes if v.get("watermark")]
    if len(yes) >= 2 or (len(votes) == 1 and yes):
        best = yes[0]
        where = best.get("where") or (hint or {}).get("where") or "center"
        # AKLĀ pārbaude (Realto 2026-10-27): prompts ar «ss.com» liek modelim to «redzēt» arī tīrā bruģī — 3× «ss.com
        # apakšā pa labi», kur nekā nav. Tāpēc uz tuvinājuma jautā BEZ jebkādas norādes: «kāds teksts/logo te ir?»;
        # ja nekāda uzraksta/logo nav → tīra.
        zoom = _crop_zoom(image_bytes, where) or crop
        blind = _blind_overlay_text(zoom) if zoom else None
        if blind is not None and not blind.get("has_overlay"):
            return {"watermark": False, "where": "", "text": "", "votes": f"{len(yes)}/{len(votes)}", "blind": "nav teksta/logo"}
        return {"watermark": True, "where": where,
                "text": (blind or {}).get("text") or best.get("text") or (hint or {}).get("text") or "",
                "votes": f"{len(yes)}/{len(votes)}"}
    return {"watermark": False, "where": "", "text": "", "votes": f"{len(yes)}/{len(votes)}"}


BLIND_PROMPT = (
    "Look at this image crop. Is there any text, lettering, logo, brand mark, URL, phone number or copyright notice "
    "that is OVERLAID on the photo (semi-transparent or opaque, added on top of the picture — not a real sign, "
    "house number or object physically in the scene)? Faint, blurred or partially erased remnants of such overlays count. "
    "Return ONLY JSON: {\"has_overlay\": true/false, \"text\": \"what you can read, or empty\"}."
)


def _blind_overlay_text(image_bytes: bytes) -> dict | None:
    """Neprimēta pārbaude: vai tuvinājumā ir KĀDS uzraksts/logo pa virsu (bez «ss.com» ieteikšanas)."""
    if not OPENAI_API_KEY:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": OPENAI_VISION_MODEL_STRICT,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": BLIND_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
        ]}],
        "max_tokens": 100,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions",
                              headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                              json=body, timeout=60, verify=_VERIFY)
        except requests.RequestException:
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            return None
        if r.status_code == 429:
            time.sleep(15)
            continue
        if r.status_code != 200:
            return None
        try:
            obj = json.loads(r.json()["choices"][0]["message"]["content"])
            return {"has_overlay": bool(obj.get("has_overlay")), "text": str(obj.get("text") or "")}
        except Exception:
            return None
    return None


def locate_watermark(image_bytes: bytes, any_agency: bool = True, hint: dict | None = None, strict: bool = False) -> dict | None:
    """Kā has_watermark_bytes, bet atdod arī KUR un KO ({"watermark", "where", "text"}) — Raimonds 2026-10-27:
    «jāapskatās, jāatrod ūdenszīme, jānoņem, jāpārbauda ar fresh eyes; ja palikusi — no jauna».
    strict=True → stiprāks modelis; hint → ko/kur meklēt (pēc tīrīšanas)."""
    if not OPENAI_API_KEY:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": OPENAI_VISION_MODEL_STRICT if strict else OPENAI_VISION_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (WM_PROMPT_ANY_AGENCY if any_agency else WM_PROMPT) + _hint_text(hint)},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high" if any_agency else "low"}},
        ]}],
        "max_tokens": 120,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions",
                              headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
                              json=body, timeout=60, verify=_VERIFY)
        except requests.RequestException:
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            return None
        if r.status_code == 429:
            time.sleep(15)
            continue
        if r.status_code != 200:
            return None
        try:
            obj = json.loads(r.json()["choices"][0]["message"]["content"])
            return {"watermark": bool(obj.get("watermark")), "where": str(obj.get("where") or ""), "text": str(obj.get("text") or "")}
        except Exception:
            return None
    return None


def has_watermark_bytes(image_bytes: bytes, any_agency: bool = False) -> bool | None:
    """Viena bilde → True (ir ss.com ūdenszīme) / False (tīra) / None (pārbaude
    neizdevās — API kļūda vai nav atslēgas; sauciens pats lemj fail-open).
    any_agency=True → skaitās arī citu aģentūru ūdenszīmes (tikai ss.lv foto pēc Seedream)."""
    if not OPENAI_API_KEY:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": OPENAI_VISION_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": WM_PROMPT_ANY_AGENCY if any_agency else WM_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                               # aģentūru logo (EfTEN pa vidu, pusdzidrs) ar «low» palika nepamanīts (2026-10-27) → high
                               "detail": "high" if any_agency else "low"}},
            ],
        }],
        "max_tokens": 100,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                         "Content-Type": "application/json"},
                json=body,
                timeout=60,
                verify=_VERIFY,
            )
        except requests.RequestException as e:
            print(f"      ! wm-check tīkla kļūda: {str(e)[:160]}")
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            return None
        if r.status_code == 429:
            print("      … wm-check 429, gaidu 15s")
            time.sleep(15)
            continue
        if r.status_code != 200:
            print(f"      ! wm-check HTTP {r.status_code}: {r.text[:200]}")
            return None
        try:
            obj = json.loads(r.json()["choices"][0]["message"]["content"])
            return bool(obj.get("watermark"))
        except Exception as e:
            print(f"      ! wm-check parse: {str(e)[:120]}")
            return None
    return None


# ---------------------------------------------------------------------------
# Kešs (pie listinga mapes, blakus _image_manifest.json)
# ---------------------------------------------------------------------------

def _load_cache(cache_path: Path) -> dict:
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data.get("images", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache_path: Path, images: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "model": OPENAI_VISION_MODEL,
            "images": images,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"      ! wm-check kešu nevar saglabāt: {e}")


def check_files(cache_dir: Path, paths: list[Path],
                force: bool = False, any_agency: bool = False) -> dict[str, bool | None]:
    """Pārbauda failus (ar disku-kešu `cache_dir/_wm_check.json`).

    Keša atslēga = filename + faila izmērs → pārrakstīta bilde (cits izmērs)
    tiek pārbaudīta no jauna pati. Atgriež {filename: True|False|None}.
    None (pārbaude neizdevās) NEkešo — nākamreiz mēģina vēlreiz.
    """
    # plašajai (aģentūru) pārbaudei savs kešs — verdikti atšķiras no ss.com-only
    cache_path = Path(cache_dir) / (CACHE_FILENAME.replace(".json", "_any.json") if any_agency else CACHE_FILENAME)
    cache = _load_cache(cache_path)
    out: dict[str, bool | None] = {}
    dirty_cache = False

    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            out[p.name] = None
            continue
        entry = cache.get(p.name)
        if (not force and entry and entry.get("size") == size
                and isinstance(entry.get("watermark"), bool)):
            out[p.name] = entry["watermark"]
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            print(f"      ! wm-check read {p.name}: {e}")
            out[p.name] = None
            continue
        if any_agency:
            res = locate_watermark(data, any_agency=True, strict=True)
            verdict = None if res is None else bool(res.get("watermark"))
        else:
            verdict = has_watermark_bytes(data)
        out[p.name] = verdict
        if verdict is not None:
            cache[p.name] = {
                "size": size,
                "watermark": verdict,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            dirty_cache = True

    if dirty_cache:
        _save_cache(cache_path, cache)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="ss.com ūdenszīmes pārbaude")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--listing", type=int, help="Pārbauda listinga ai_ready bildes")
    g.add_argument("--files", help="Komatu atdalīti faila ceļi (bez keša)")
    ap.add_argument("--force", action="store_true", help="Ignorē kešu")
    args = ap.parse_args()

    if args.listing:
        base = STORAGE_ROOT / "listings" / str(args.listing)
        ai_dir = base / "ai_ready"
        exts = {".jpg", ".jpeg", ".png", ".webp"}
        paths = sorted(p for p in ai_dir.glob("img_*.*")
                       if p.suffix.lower() in exts) if ai_dir.is_dir() else []
        if not paths:
            print(f"Nav ai_ready bilžu ({ai_dir})")
            sys.exit(0)
        verdicts = check_files(base, paths, force=args.force)
    else:
        paths = [Path(s.strip()) for s in args.files.split(",") if s.strip()]
        verdicts = {}
        for p in paths:
            verdicts[p.name] = has_watermark_bytes(p.read_bytes())

    bad = [n for n, v in verdicts.items() if v is True]
    for n, v in verdicts.items():
        tag = {True: "!! ŪDENSZĪME", False: "   tīra", None: " ? neizdevās"}[v]
        print(f"  {tag}  {n}")
    print(f"\nKopā: {len(verdicts)} bildes, ar ūdenszīmi: {len(bad)}")
    sys.exit(2 if bad else 0)


if __name__ == "__main__":
    main()
