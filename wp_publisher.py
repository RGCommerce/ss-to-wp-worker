"""WP REST publisher — Melnas kastes rgc-mk/v3 endpoint klients (Etaps 3.3).

Token-based (X-RGC-Token header) — apiet Application Password 401 (Bloketajs 1)
un Houzez property REST READ-only (Bloketajs 2). Plugin: rgc-melna-kaste-endpoints-v3.

Lietosana:
    from wp_publisher import WPPublisher
    wp = WPPublisher()
    wp.health()
    pid = wp.create_property(title="Brivibas 30", status="draft",
                             meta={"fave_property_price": "1500"})["id"]
    aid = wp.upload_media("/path/to/img.jpg", post_id=pid)["id"]
    tid = wp.ensure_term("property_city", "Riga")["term_id"]
    wp.update_property(pid, taxonomies={"property_city": [tid]},
                       featured_media=aid)
    wp.rebuild_multi_units(pid)
    wp.delete_property(pid, force=True)

Env (.env): WP_URL, RGC_MK_TOKEN. Opcionals WP_VERIFY_SSL=0 lokalai testesanai.
"""
from __future__ import annotations

import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(Path(__file__).parent / ".env")


class WPPublisherError(RuntimeError):
    """REST izsaukums neizdevas (ne-2xx vai tikla kluda)."""


class WPPublisher:
    """rgc-mk/v3 endpoint klients ar token auth."""

    NAMESPACE = "rgc-mk/v5"

    def __init__(
        self,
        wp_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self.wp_url = (wp_url or os.getenv("WP_URL") or "").rstrip("/")
        self.token = token or os.getenv("RGC_MK_TOKEN")
        self.timeout = timeout
        # Lokali aiz korporativa proxy CA verifikacija var salust; produkcija = True.
        self.verify = os.getenv("WP_VERIFY_SSL", "1") not in ("0", "false", "False")

        if not self.wp_url:
            raise WPPublisherError("Trukst WP_URL (.env)")
        if not self.token:
            raise WPPublisherError("Trukst RGC_MK_TOKEN (.env)")

        self._base = f"{self.wp_url}/wp-json/{self.NAMESPACE}"
        self._session = requests.Session()
        self._session.headers["X-RGC-Token"] = self.token

    # ---- iekseja helperis -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        data: Optional[bytes] = None,
        headers: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        url = self._base + path
        try:
            resp = self._session.request(
                method,
                url,
                json=json_body,
                data=data,
                headers=headers,
                timeout=timeout or self.timeout,
                verify=self.verify,
            )
        except requests.RequestException as exc:
            raise WPPublisherError(f"{method} {url} tikla kluda: {exc}") from exc

        if not resp.ok:
            raise WPPublisherError(
                f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError:
            raise WPPublisherError(
                f"{method} {url} atbilde nav JSON: {resp.text[:300]}"
            )

    # ---- health -----------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    # ---- property ---------------------------------------------------------

    def create_property(
        self,
        *,
        title: str,
        content: Optional[str] = None,
        excerpt: Optional[str] = None,
        status: str = "draft",
        author: Optional[int] = None,
        meta: Optional[dict] = None,
        taxonomies: Optional[dict] = None,
        featured_media: Optional[int] = None,
        floor_plan_attachment_ids: Optional[list[int]] = None,
        floor_plan_title: Optional[str] = None,
        geocode_address: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"title": title, "status": status}
        if content is not None:
            body["content"] = content
        if excerpt is not None:
            body["excerpt"] = excerpt
        if author is not None:
            body["author"] = author
        if meta:
            body["meta"] = meta
        if taxonomies:
            body["taxonomies"] = taxonomies
        if featured_media is not None:
            body["featured_media"] = featured_media
        if floor_plan_attachment_ids is not None:
            body["floor_plan_attachment_ids"] = [
                int(x) for x in floor_plan_attachment_ids]
        if floor_plan_title:
            body["floor_plan_title"] = floor_plan_title
        # PILNA ģeokodējamā adrese (tikai plugin koordinātēm; NErādās frontendā).
        if geocode_address:
            body["geocode_address"] = geocode_address
        return self._request("POST", "/property/create", json_body=body)

    def update_property(
        self,
        post_id: int,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        excerpt: Optional[str] = None,
        status: Optional[str] = None,
        author: Optional[int] = None,
        meta: Optional[dict] = None,
        taxonomies: Optional[dict] = None,
        featured_media: Optional[int] = None,
        floor_plan_attachment_ids: Optional[list[int]] = None,
        floor_plan_title: Optional[str] = None,
        force_text: bool = False,
        geocode_address: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if content is not None:
            body["content"] = content
        if excerpt is not None:
            body["excerpt"] = excerpt
        if status is not None:
            body["status"] = status
        if author is not None:
            body["author"] = author
        if meta:
            body["meta"] = meta
        if taxonomies:
            body["taxonomies"] = taxonomies
        if featured_media is not None:
            body["featured_media"] = featured_media
        if floor_plan_attachment_ids is not None:
            body["floor_plan_attachment_ids"] = [
                int(x) for x in floor_plan_attachment_ids]
        if floor_plan_title:
            body["floor_plan_title"] = floor_plan_title
        if force_text:
            body["force_text"] = True
        # PILNA ģeokodējamā adrese (tikai plugin koordinātēm; NErādās frontendā).
        if geocode_address:
            body["geocode_address"] = geocode_address
        return self._request(
            "POST", f"/property/{int(post_id)}/update", json_body=body
        )

    def delete_property(self, post_id: int, force: bool = False) -> dict:
        suffix = "?force=true" if force else ""
        return self._request("DELETE", f"/property/{int(post_id)}{suffix}")

    def rebuild_multi_units(
        self, post_id: int, exclude_self: bool = True
    ) -> dict:
        return self._request(
            "POST",
            f"/property/{int(post_id)}/rebuild-multi-units",
            json_body={"exclude_self": exclude_self},
        )

    # ---- media ------------------------------------------------------------

    def upload_media(
        self,
        image_path: str | Path,
        *,
        post_id: Optional[int] = None,
        filename: Optional[str] = None,
        alt: Optional[str] = None,
    ) -> dict:
        p = Path(image_path)
        if not p.is_file():
            raise WPPublisherError(f"Bilde nav atrasta: {p}")
        fn = filename or p.name
        ctype = mimetypes.guess_type(fn)[0] or "image/jpeg"
        qs = f"?filename={requests.utils.quote(fn)}"
        if post_id is not None:
            qs += f"&post_id={int(post_id)}"
        if alt:
            qs += f"&alt={requests.utils.quote(alt)}"
        return self._request(
            "POST",
            "/media" + qs,
            data=p.read_bytes(),
            headers={"Content-Type": ctype},
            timeout=180,
        )

    # ---- term -------------------------------------------------------------

    def ensure_term(
        self,
        taxonomy: str,
        name: str,
        parent_name: Optional[str] = None,
        icon_type: Optional[str] = None,
        icon_image_id: Optional[int] = None,
    ) -> dict:
        """Lookup-or-create taksonomijas term. icon_type + icon_image_id
        (opcionāli) — plugin tos iestata TIKAI jaunizveidotam term-am
        (property_feature ikona); esošam term-am icon netiek aiztikts."""
        body: dict[str, Any] = {"taxonomy": taxonomy, "name": name}
        if parent_name:
            body["parent_name"] = parent_name
        if icon_type:
            body["icon_type"] = icon_type
        if icon_image_id:
            body["icon_image_id"] = int(icon_image_id)
        return self._request("POST", "/term", json_body=body)


if __name__ == "__main__":
    # Maza saniti parbaude (tikai health, neko neizveido).
    wp = WPPublisher()
    print("WP:", wp.wp_url, "| namespace:", wp.NAMESPACE,
          "| verify_ssl:", wp.verify)
    print(wp.health())
