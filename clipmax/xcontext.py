"""Contexto del día en X (Twitter) sobre el evento.

La API de lectura de X no es gratis, así que hay tres modos (config x.modo):
- "manual" (por defecto, $0): pegas en la interfaz web los posts/hilos que viste
  en X, o dejas archivos .txt en data/sesiones/<fecha>/x/. ClipMax los guarda,
  extrae los temas y se los pasa a Claude.
- "api": si tienes un token Bearer de X con acceso de lectura, consulta la
  búsqueda reciente cada N minutos respetando un máximo diario de consultas.
- "claude_web": al procesar el día, Claude usa su herramienta de búsqueda web
  para resumir qué se comenta del evento (cuesta ~$0.01 por búsqueda + tokens).

Además de dar contexto a Claude, los temas de X suben la puntuación de los
candidatos cuya transcripción toca esos mismos temas.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import session_dir
from .db import Database
from .mentions import normalize

log = logging.getLogger(__name__)

STOPWORDS = set("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque bien cada casi como con
contra cual cuales cuando de del desde donde dos el ella ellas ello ellos en entre era eran eres es esa
esas ese eso esos esta estaba estado estan estar este esto estos estoy fue fueron ha hace hacer hacia han
hasta hay la las le les lo los mas me mi mis mucho muy nada ni no nos nosotros o otra otras otro otros
para pero poco por porque que quien se sea segun ser si sido siempre sin sobre solo son su sus tal
tambien tan tanto te tiene tienen toda todas todo todos tu tus un una uno unos usted va van vamos ya yo
the and for with you this that are https http co www rt amp via jaja jajaja xd q k pa pq x vs
""".split())

_URL_RE = re.compile(r"https?://\S+")
_STATUS_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)")


def _hash_id(text: str) -> str:
    return "h" + hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def parse_manual_text(text: str) -> list[dict]:
    """Divide texto pegado en posts: un post por bloque separado por línea en blanco.

    Si un bloque trae un enlace x.com/<usuario>/status/<id>, se usan como autor e id.
    Si empieza con @usuario:, ese es el autor.
    """
    posts = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if len(block) < 3:
            continue
        autor, url, ext_id = None, None, None
        status = _STATUS_RE.search(block)
        if status:
            autor, ext_id = "@" + status.group(1), "x" + status.group(2)
            url = f"https://x.com/{status.group(1)}/status/{status.group(2)}"
        at = re.match(r"^(@\w{2,30})\s*[:\-–]\s*", block)
        if at:
            autor = autor or at.group(1)
            block = block[at.end():]
        clean = _URL_RE.sub("", block).strip()
        if not clean:
            continue
        posts.append({"ext_id": ext_id or _hash_id(clean), "autor": autor, "texto": clean[:2000],
                      "url": url, "created_at": None, "likes": 0})
    return posts


def import_folder(cfg: dict, db: Database, session: dict) -> int:
    """Importa .txt/.md que el usuario dejó en data/sesiones/<fecha>/x/."""
    folder = session_dir(cfg, session["fecha"]) / "x"
    folder.mkdir(exist_ok=True)
    added = 0
    for f in sorted(folder.glob("*")):
        if f.suffix.lower() in (".txt", ".md"):
            added += db.add_x_posts(session["id"], parse_manual_text(f.read_text("utf-8", "replace")), "archivo")
        elif f.suffix.lower() == ".json":
            try:
                data = json.loads(f.read_text("utf-8"))
                items = data if isinstance(data, list) else data.get("posts", [])
                posts = [{"ext_id": str(p.get("id") or _hash_id(p.get("texto") or p.get("text", ""))),
                          "autor": p.get("autor") or p.get("author"),
                          "texto": p.get("texto") or p.get("text", ""),
                          "url": p.get("url"), "created_at": None,
                          "likes": p.get("likes", 0)} for p in items if (p.get("texto") or p.get("text"))]
                added += db.add_x_posts(session["id"], posts, "archivo")
            except (ValueError, AttributeError) as exc:
                log.warning("No pude leer %s: %s", f.name, exc)
    return added


def keywords(texts: list[str], top: int = 25) -> list[tuple[str, int]]:
    """Temas más repetidos (palabras y pares de palabras) en los posts."""
    counter: collections.Counter = collections.Counter()
    for t in texts:
        words = [w for w in normalize(_URL_RE.sub("", t)).split()
                 if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]
        counter.update(set(words))
        counter.update({f"{a} {b}" for a, b in zip(words, words[1:])})
    # Un bigrama solo vale si aparece al menos 2 veces.
    items = [(k, v) for k, v in counter.items() if v >= 2 or " " not in k]
    items.sort(key=lambda kv: (-kv[1], -len(kv[0])))
    return items[:top]


def context_text(db: Database, session: dict, cfg: dict) -> str:
    """Texto del contexto de X que va en el paquete para Claude (recortado)."""
    max_chars = int(cfg["x"]["max_caracteres_contexto"])
    parts = []
    if (session.get("x_contexto") or "").strip():
        parts.append(session["x_contexto"].strip())
    for p in db.x_posts(session["id"]):
        if p["fuente"] == "manual" and session.get("x_contexto") and p["texto"] in session["x_contexto"]:
            continue
        who = p["autor"] or "anónimo"
        likes = f" ({p['likes']} likes)" if p["likes"] else ""
        parts.append(f"- {who}{likes}: {p['texto']}")
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n[...recortado]"
    return text


class XApiPoller(threading.Thread):
    """Consulta opcional a la API v2 de X (búsqueda reciente) con token Bearer."""

    URL = "https://api.x.com/2/tweets/search/recent"

    def __init__(self, cfg: dict, db: Database, session: dict):
        super().__init__(name="x-poller", daemon=True)
        self.cfg = cfg
        self.db = db
        self.session = session
        self.token = os.environ.get("X_BEARER_TOKEN", "").strip()
        self._stop_evt = threading.Event()
        self.calls = 0
        self.last_error = ""

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def status(self) -> dict:
        return {"consultas": self.calls, "error": self.last_error, "token": bool(self.token)}

    def run(self) -> None:
        if not self.token:
            self.last_error = "falta X_BEARER_TOKEN en .env"
            log.warning("x.modo=api pero no hay X_BEARER_TOKEN; no se consultará X")
            return
        interval = max(15, int(self.cfg["x"]["intervalo_min"])) * 60
        max_calls = int(self.cfg["x"]["max_consultas_dia"])
        while not self._stop_evt.is_set() and self.calls < max_calls:
            try:
                n = self.poll_once()
                log.info("X: %d posts nuevos", n)
            except urllib.error.HTTPError as exc:
                self.last_error = f"HTTP {exc.code}"
                log.warning("X API respondió %s; %s", exc.code,
                            "cuota agotada o plan sin lectura" if exc.code in (402, 403, 429) else "")
                if exc.code in (401, 402, 403):
                    return
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("X API falló: %s", exc)
            self._stop_evt.wait(interval)

    def poll_once(self) -> int:
        params = {
            "query": self.cfg["x"]["consulta"],
            "max_results": "50",
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        req = urllib.request.Request(
            f"{self.URL}?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "ClipMax/1.0"},
        )
        self.calls += 1
        with urllib.request.urlopen(req, timeout=30) as fh:
            data = json.loads(fh.read().decode("utf-8"))
        users = {u["id"]: u["username"] for u in (data.get("includes") or {}).get("users", [])}
        posts = []
        for t in data.get("data", []):
            user = users.get(t.get("author_id"), "")
            created = None
            if t.get("created_at"):
                created = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).timestamp()
            posts.append({
                "ext_id": "x" + t["id"], "autor": f"@{user}" if user else None, "texto": t.get("text", ""),
                "url": f"https://x.com/{user or 'i'}/status/{t['id']}", "created_at": created,
                "likes": (t.get("public_metrics") or {}).get("like_count", 0),
            })
        return self.db.add_x_posts(self.session["id"], posts, "api")


def save_manual_context(db: Database, session: dict, text: str) -> int:
    """Guarda lo pegado en la web: el texto completo + los posts individuales."""
    db.update_session(session["id"], x_contexto=text.strip())
    db.delete_x_posts(session["id"], "manual")
    return db.add_x_posts(session["id"], parse_manual_text(text), "manual")


def x_dir(cfg: dict, fecha: str) -> Path:
    d = session_dir(cfg, fecha) / "x"
    d.mkdir(exist_ok=True)
    return d


def burst_times(db: Database, session: dict, window_s: int = 600) -> list[tuple[float, int]]:
    """Ventanas de 10 min con más posts (solo para posts con hora, p. ej. los de la API)."""
    times = [p["created_at"] for p in db.x_posts(session["id"]) if p["created_at"]]
    if not times:
        return []
    buckets = collections.Counter(int(t // window_s) * window_s for t in times)
    avg = sum(buckets.values()) / max(1, len(buckets))
    return sorted(((float(k), v) for k, v in buckets.items() if v >= max(3, 2 * avg)), key=lambda kv: kv[0])

