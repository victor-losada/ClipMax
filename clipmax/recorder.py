"""Grabación de streams de Kick.

Diseño:
- Un hilo StreamRecorder por streamer activo.
- Cada conexión se graba con ffmpeg en *copia directa* (sin recodificar, casi
  0 % de CPU) a un archivo MPEG-TS. El TS sobrevive a cortes de luz o cierres
  bruscos: si ffmpeg muere, lo grabado hasta ese segundo es válido (un MP4 a
  medio escribir, en cambio, queda inservible).
- Si el stream se cae o se reinicia, se abre una "parte" nueva. Cada parte
  guarda en SQLite la hora de pared en la que empezó: así cualquier señal del
  chat (que también tiene hora de pared) se traduce a un segundo exacto del video.
- Al cerrar el día, finalize_recordings() concatena las partes en un único MP4
  por streamer (también sin recodificar) y guarda el desfase de cada parte.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import tools
from .config import session_dir
from .db import Database
from .kick import KickClient, KickError
from .winutil import POPEN_FLAGS

log = logging.getLogger(__name__)


# Segundos de grabación tras los cuales se mide cuánto video "viejo" trajo el arranque.
LEAD_PROBE_S = 20.0


def recordings_dir(cfg: dict, fecha: str, slug: str | None = None) -> Path:
    d = session_dir(cfg, fecha) / "grabaciones"
    if slug:
        d = d / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


class StreamRecorder(threading.Thread):
    def __init__(self, cfg: dict, db: Database, session: dict, streamer: dict, kick: KickClient):
        super().__init__(name=f"rec-{streamer['slug']}", daemon=True)
        self.cfg = cfg
        self.db = db
        self.session = session
        self.streamer = streamer
        self.slug = streamer["slug"]
        self.kick = kick
        self._stop_evt = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=15)
        self._lock = threading.Lock()
        self._status: dict = {"estado": "iniciando", "detalle": "", "parte": None, "bytes": 0,
                              "desde": None, "titulo": "", "espectadores": 0}

    # -- estado para la interfaz ------------------------------------------------
    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def current_part(self) -> dict | None:
        """Parte que se está escribiendo ahora (la usa el transcriptor en vivo)."""
        return self.status.get("parte")

    # -- ciclo principal ------------------------------------------------------------
    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        wait = float(self.cfg["grabacion"]["reintento_s"])
        while not self._stop_evt.is_set():
            try:
                info = self.kick.get_channel(self.slug, use_cache=False)
            except Exception as exc:  # noqa: BLE001 - red caída, Cloudflare, etc.
                self._set(estado="error", detalle=f"API de Kick: {exc}")
                log.warning("[%s] no pude consultar el canal: %s", self.slug, exc)
                self._stop_evt.wait(wait)
                continue
            self._set(titulo=info.title, espectadores=info.viewers)
            if not info.is_live:
                self._set(estado="offline", detalle="esperando a que salga en vivo", parte=None)
                self._stop_evt.wait(wait)
                continue
            try:
                url = self._resolve_stream(info)
            except Exception as exc:  # noqa: BLE001
                self._set(estado="error", detalle=f"no pude resolver el stream: {exc}")
                log.warning("[%s] no pude resolver la URL HLS: %s", self.slug, exc)
                self._stop_evt.wait(wait)
                continue
            self._record_part(url)
            if not self._stop_evt.is_set():
                # Corte del stream o de la red: breve pausa y se abre otra parte.
                self._stop_evt.wait(5)
        self._set(estado="detenido", detalle="", parte=None)

    def _resolve_stream(self, info) -> str:
        max_h = int(self.cfg["grabacion"]["calidad_max"])
        if self.cfg["grabacion"]["resolver"] == "yt-dlp":
            try:
                cmd = tools.ytdlp_cmd() + [
                    "-g", "--no-warnings", "-f", f"best[height<={max_h}]/best", self.streamer["url"],
                ]
                out = tools.run(cmd, timeout=90).stdout.decode("utf-8", "replace").strip().splitlines()
                urls = [u for u in out if u.startswith("http")]
                if urls:
                    return urls[0]
                raise RuntimeError("yt-dlp no devolvió URL")
            except Exception as exc:  # noqa: BLE001
                log.info("[%s] yt-dlp falló (%s); uso la playback_url de la API", self.slug, exc)
        if not info.playback_url:
            raise KickError("la API no trajo playback_url")
        return self.kick.resolve_variant(info.playback_url, max_h)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _record_part(self, url: str) -> None:
        fecha = self.session["fecha"]
        idx = self.db.next_part_index(self.session["id"], self.slug)
        path = recordings_dir(self.cfg, fecha, self.slug) / f"{self.slug}_parte{idx:03d}.ts"
        cmd = [
            tools.ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-rw_timeout", "20000000",          # 20 s sin datos -> ffmpeg sale y reconectamos
            "-i", url,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c", "copy", "-f", "mpegts", str(path),
        ]
        log.info("[%s] grabando parte %d -> %s", self.slug, idx, path.name)
        self._stderr_tail.clear()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=POPEN_FLAGS)
        self._proc = proc
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

        # Esperamos los primeros bytes: esa es la hora de pared del segundo 0 del archivo.
        t_launch = time.time()
        while proc.poll() is None and not self._stop_evt.is_set():
            if path.exists() and path.stat().st_size > 0:
                break
            if time.time() - t_launch > 60:
                break
            time.sleep(0.25)
        if not path.exists() or path.stat().st_size == 0:
            self._shutdown(proc)
            err = " | ".join(self._stderr_tail) or "sin datos"
            self._set(estado="error", detalle=f"ffmpeg no recibió datos: {err[-300:]}")
            log.warning("[%s] ffmpeg no recibió datos: %s", self.slug, err[-500:])
            path.unlink(missing_ok=True)
            return

        started_at = time.time()
        part = self.db.add_part(self.session["id"], self.slug, str(path), started_at)
        self._set(estado="grabando", detalle="", desde=started_at,
                  parte={"id": part["id"], "path": str(path), "started_at": started_at,
                         "index": part["part_index"]})

        stale_limit = float(self.cfg["grabacion"]["estancado_s"])
        last_size, last_growth = 0, time.time()
        lead_checked = False
        while proc.poll() is None:
            if self._stop_evt.wait(2):
                break
            if not lead_checked and time.time() - started_at >= LEAD_PROBE_S:
                lead_checked = True
                started_at = self._correct_start(part, path, started_at)
            size = path.stat().st_size if path.exists() else 0
            if size > last_size:
                last_size, last_growth = size, time.time()
                self._set(bytes=size)
            elif time.time() - last_growth > stale_limit:
                log.warning("[%s] la grabación no crece hace %ds; reinicio ffmpeg", self.slug, stale_limit)
                break
        self._shutdown(proc)
        size = path.stat().st_size if path.exists() else 0
        self.db.close_part(part["id"], last_growth, size)
        if proc.returncode not in (0, None, 255) and self._stderr_tail:
            log.info("[%s] ffmpeg terminó (%s): %s", self.slug, proc.returncode, self._stderr_tail[-1])
        self._set(parte=None, estado="reconectando" if not self._stop_evt.is_set() else "detenido")

    def _correct_start(self, part: dict, path: Path, started_at: float) -> float:
        """Ajusta la hora de pared del segundo 0 del archivo.

        Al conectarse a un directo HLS, ffmpeg baja de golpe los últimos segmentos de la lista
        (en Kick, ~15-20 s de video ya emitido). Así, el segundo 0 del archivo es anterior a la
        hora en que llegaron los primeros bytes y, sin corregirlo, cada clip salía corrido esos
        segundos respecto al chat (se podía perder el remate). Se mide una vez: duración del
        archivo menos tiempo de reloj transcurrido.
        """
        try:
            media = tools.probe_duration(path)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] no pude medir el arranque: %s", self.slug, exc)
            return started_at
        lead = media - (time.time() - started_at)
        if not (1.0 <= lead <= 90.0):
            return started_at
        new_start = started_at - lead
        self.db.update_part(part["id"], started_at=new_start)
        parte = dict(self.status.get("parte") or {})
        if parte.get("id") == part["id"]:
            parte["started_at"] = new_start
            self._set(desde=new_start, parte=parte)
        log.info("[%s] el directo arrancó con %.1fs ya emitidos; corrijo la hora de inicio de la parte",
                 self.slug, lead)
        return new_start

    def _shutdown(self, proc: subprocess.Popen) -> None:
        """Cierre ordenado: 'q' por stdin (ffmpeg cierra el archivo bien); si no, kill."""
        if proc.poll() is not None:
            return
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Post-grabación: un MP4 por streamer y mapa hora-de-pared -> segundo del video
# ---------------------------------------------------------------------------

def finalize_recordings(cfg: dict, db: Database, session: dict) -> dict[str, list[str]]:
    """Concatena las partes .ts de cada streamer en un MP4. Idempotente."""
    results: dict[str, list[str]] = {}
    parts_all = db.list_parts(session["id"])
    slugs = sorted({p["slug"] for p in parts_all})
    for slug in slugs:
        parts = db.list_parts(session["id"], slug)
        done = sorted({p["final_path"] for p in parts
                       if p["final_path"] and Path(p["final_path"]).exists()})
        # Solo se procesan las partes que aún no están dentro de un MP4 final
        # (si la sesión se reanudó después de finalizar, las nuevas van a otro MP4).
        pending = [p for p in parts if not (p["final_path"] and Path(p["final_path"]).exists())]
        results[slug] = done
        if not pending:
            continue
        usable = []
        for p in pending:
            if not Path(p["path"]).exists():
                log.warning("[%s] falta la parte %s; la omito", slug, p["path"])
                continue
            dur = tools.probe_duration(p["path"])
            db.update_part(p["id"], duration=dur)
            p["duration"] = dur
            if dur < 1:
                log.info("[%s] parte %d vacía; la descarto", slug, p["part_index"])
                continue
            usable.append(p)
        if not usable:
            continue
        suffix = "" if not done else f"_desde_parte{usable[0]['part_index']:03d}"
        final = recordings_dir(cfg, session["fecha"]) / f"{session['fecha']}_{slug}{suffix}.mp4"
        results[slug] = done + _concat_parts(cfg, db, usable, final)
    return results


def _concat_parts(cfg: dict, db: Database, parts: list[dict], final: Path) -> list[str]:
    list_file = final.with_suffix(".txt")
    list_file.write_text("".join(tools.concat_list_line(p["path"]) for p in parts), encoding="utf-8")
    tmp = final.with_suffix(".tmp.mp4")
    try:
        tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", str(list_file),
                   "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                   "-movflags", "+faststart", str(tmp)], timeout=6 * 3600)
        expected = sum(p["duration"] for p in parts)
        got = tools.probe_duration(tmp)
        if abs(got - expected) > max(5.0, 0.02 * expected):
            raise RuntimeError(f"duración inesperada ({got:.0f}s vs {expected:.0f}s)")
        tmp.replace(final)
        offset = 0.0
        for p in parts:
            db.update_part(p["id"], final_path=str(final), offset_in_final=offset)
            offset += p["duration"]
        outputs = [str(final)]
    except Exception as exc:  # noqa: BLE001
        # Plan B: cada parte a su propio MP4 (p. ej. si cambió la resolución a mitad del día).
        log.warning("Concatenación falló (%s); remuxeo cada parte por separado", exc)
        tmp.unlink(missing_ok=True)
        outputs = []
        for p in parts:
            out = Path(p["path"]).with_suffix(".mp4")
            tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", p["path"],
                       "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                       "-movflags", "+faststart", str(out)], timeout=3 * 3600)
            db.update_part(p["id"], final_path=str(out), offset_in_final=0.0)
            outputs.append(str(out))
    finally:
        list_file.unlink(missing_ok=True)
    if cfg["grabacion"]["borrar_partes_ts"]:
        for p in parts:
            try:
                os.remove(p["path"])
            except OSError:
                pass
    log.info("Grabación final lista: %s", ", ".join(Path(o).name for o in outputs))
    return outputs


def _part_end(p: dict) -> float:
    if p.get("duration"):
        return p["started_at"] + p["duration"]
    if p.get("ended_at"):
        return p["ended_at"]
    return time.time()  # parte en curso


def _media_path(p: dict) -> tuple[str, float]:
    """(archivo, desfase del inicio de la parte dentro de ese archivo)."""
    if p.get("final_path") and Path(p["final_path"]).exists():
        return p["final_path"], float(p.get("offset_in_final") or 0.0)
    return p["path"], 0.0


def locate_range(db: Database, session_id: int, slug: str, t0: float, t1: float,
                 chat_offset: float = 0.0) -> tuple[str, float, float, float] | None:
    """Traduce el intervalo de pared [t0, t1] a (archivo, segundo_inicio, duración, t0_efectivo).

    Si el intervalo cruza dos partes, se recorta a la parte que contiene más
    tiempo del intervalo (los cortes entre partes son reconexiones, sin contenido).
    t0_efectivo es la hora de pared (en el mismo reloj que t0) donde realmente
    empieza el tramo devuelto, por si se recortó al inicio de la parte.
    """
    t0 += chat_offset
    t1 += chat_offset
    best, best_overlap = None, 0.0
    for p in db.list_parts(session_id, slug):
        start, end = p["started_at"], _part_end(p)
        overlap = min(end, t1) - max(start, t0)
        if overlap > best_overlap:
            best, best_overlap = p, overlap
    if not best or best_overlap < 1.0:
        return None
    path, base = _media_path(best)
    if not Path(path).exists():
        return None
    a = max(t0, best["started_at"])
    b = min(t1, _part_end(best))
    return path, base + (a - best["started_at"]), b - a, a - chat_offset


def grab_snapshot(cfg: dict, kick: KickClient, streamer: dict, out: Path) -> Path:
    """Guarda un fotograma del stream en vivo (para calibrar el recorte de la cámara)."""
    info = kick.get_channel(streamer["slug"], use_cache=False)
    if not info.is_live or not info.playback_url:
        raise KickError(f"{streamer['nombre']} no está en vivo")
    url = kick.resolve_variant(info.playback_url, 1080)
    out.parent.mkdir(parents=True, exist_ok=True)
    tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", url,
               "-frames:v", "1", "-q:v", "3", str(out)], timeout=90)
    return out


def snapshot_from_file(path: str, offset: float, out: Path, width: int | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{max(0.0, offset):.3f}", "-i", str(path), "-frames:v", "1"]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    tools.run(cmd + ["-q:v", "3", str(out)], timeout=120)
    return out
