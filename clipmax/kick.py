"""Cliente mínimo de Kick.

Kick protege https://kick.com/api/v2/channels/<slug> con Cloudflare. Un
`requests` normal recibe 403; curl_cffi imita la huella TLS de Chrome y pasa
sin cuentas ni cookies (yt-dlp usa exactamente el mismo truco). De esa respuesta
salen: si está en vivo, la URL HLS (playback_url) y el id del chatroom.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin

log = logging.getLogger(__name__)

API_CHANNEL = "https://kick.com/api/v2/channels/{slug}"


@dataclass
class ChannelInfo:
    slug: str
    channel_id: int | None
    chatroom_id: int | None
    is_live: bool
    playback_url: str | None
    title: str
    viewers: int
    livestream_id: int | None


class KickError(RuntimeError):
    pass


class KickClient:
    """Thread-safe; cachea respuestas unos segundos para no martillar la API."""

    def __init__(self, cache_s: float = 20.0):
        self._cache: dict[str, tuple[float, ChannelInfo]] = {}
        self._lock = threading.Lock()
        self._cache_s = cache_s
        self._session = None

    def _http(self):
        if self._session is None:
            try:
                from curl_cffi import requests as creq

                self._session = creq.Session(impersonate="chrome", timeout=20)
            except ImportError:  # pragma: no cover - curl_cffi viene con yt-dlp[curl-cffi]
                import urllib.request

                log.warning("curl_cffi no está instalado; Cloudflare probablemente bloqueará la API de Kick")
                self._session = urllib.request
        return self._session

    def _get_json(self, url: str) -> dict:
        http = self._http()
        if hasattr(http, "get"):
            resp = http.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 404:
                raise KickError(f"El canal no existe: {url}")
            if resp.status_code != 200:
                raise KickError(f"Kick respondió {resp.status_code} para {url}")
            return resp.json()
        import json

        req = http.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        with http.urlopen(req, timeout=20) as fh:  # type: ignore[attr-defined]
            return json.loads(fh.read().decode("utf-8"))

    def _get_text(self, url: str) -> str:
        http = self._http()
        if hasattr(http, "get"):
            resp = http.get(url)
            if resp.status_code != 200:
                raise KickError(f"HTTP {resp.status_code} al leer {url}")
            return resp.text
        with http.urlopen(url, timeout=20) as fh:  # type: ignore[attr-defined]
            return fh.read().decode("utf-8", "replace")

    def get_channel(self, slug: str, use_cache: bool = True) -> ChannelInfo:
        with self._lock:
            hit = self._cache.get(slug)
            if use_cache and hit and time.time() - hit[0] < self._cache_s:
                return hit[1]
        data = self._get_json(API_CHANNEL.format(slug=slug))
        live = data.get("livestream") or None
        info = ChannelInfo(
            slug=slug,
            channel_id=data.get("id"),
            chatroom_id=(data.get("chatroom") or {}).get("id"),
            is_live=bool(live) and bool(live.get("is_live", True)),
            playback_url=data.get("playback_url"),
            title=(live or {}).get("session_title") or "",
            viewers=int((live or {}).get("viewer_count") or 0),
            livestream_id=(live or {}).get("id"),
        )
        with self._lock:
            self._cache[slug] = (time.time(), info)
        return info

    def resolve_variant(self, master_url: str, max_height: int) -> str:
        """Elige de la playlist maestra HLS la mejor variante con altura <= max_height."""
        text = self._get_text(master_url)
        return pick_variant(text, master_url, max_height)


_RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)")
_BW_RE = re.compile(r"BANDWIDTH=(\d+)")


def pick_variant(master_text: str, base_url: str, max_height: int) -> str:
    """Parser mínimo de playlist maestra. Si no es maestra, devuelve la misma URL."""
    lines = [ln.strip() for ln in master_text.splitlines()]
    variants: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = next((ln for ln in lines[i + 1:] if ln and not ln.startswith("#")), None)
        if not uri:
            continue
        res = _RES_RE.search(line)
        bw = _BW_RE.search(line)
        height = int(res.group(2)) if res else 0
        variants.append((height, int(bw.group(1)) if bw else 0, urljoin(base_url, uri)))
    if not variants:
        return base_url
    ok = [v for v in variants if 0 < v[0] <= max_height]
    if not ok:
        with_height = [v for v in variants if v[0] > 0]
        # Todas más altas que el límite -> la más pequeña; sin RESOLUTION -> mayor bitrate.
        ok = [min(with_height)] if with_height else [max(variants, key=lambda v: v[1])]
    return max(ok)[2]
