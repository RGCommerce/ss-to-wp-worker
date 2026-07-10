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


# ---------------------------------------------------------------------------
# Vision call
# ---------------------------------------------------------------------------

def has_watermark_bytes(image_bytes: bytes) -> bool | None:
    """Viena bilde → True (ir ss.com ūdenszīme) / False (tīra) / None (pārbaude
    neizdevās — API kļūda vai nav atslēgas; sauciens pats lemj fail-open)."""
    if not OPENAI_API_KEY:
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": OPENAI_VISION_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": WM_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                               "detail": "low"}},
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
                force: bool = False) -> dict[str, bool | None]:
    """Pārbauda failus (ar disku-kešu `cache_dir/_wm_check.json`).

    Keša atslēga = filename + faila izmērs → pārrakstīta bilde (cits izmērs)
    tiek pārbaudīta no jauna pati. Atgriež {filename: True|False|None}.
    None (pārbaude neizdevās) NEkešo — nākamreiz mēģina vēlreiz.
    """
    cache_path = Path(cache_dir) / CACHE_FILENAME
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
