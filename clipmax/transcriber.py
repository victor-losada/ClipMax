"""Transcripción local con whisper.cpp (whisper-cli.exe). Cero costo, cero nube.

Dos usos:
1. En vivo (LiveTranscriber): solo para los streamers con `transcribir_en_vivo`
   (por defecto la pareja principal). Cada minuto toma el último tramo grabado,
   lo transcribe con un modelo rápido (base) y busca si nombran al otro
   streamer. Eso genera señales "mencion_voz" casi en tiempo real.
2. Por ventana (transcribe_moments): después del evento, transcribe con un
   modelo mejor (small/medium) solo las ventanas candidatas que van a Claude.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import tools
from .config import active_streamers, resolve_path
from .db import Database
from .mentions import MentionMatcher, normalize

log = logging.getLogger(__name__)

# Frases que whisper "alucina" sobre música o ruido de juego (muy típicas en español).
_HALLUCINATIONS = (
    "amara org", "subtitulos realizados por", "subtitulos por la comunidad", "gracias por ver",
    "suscribete", "no olvides suscribirte", "gracias por su atencion", "musica de fondo",
)
_ONLY_NOISE = {"musica", "music", "aplausos", "risas de fondo", "silencio", "", "sonido", "ruido"}


@dataclass
class Segment:
    start: float  # segundos relativos al inicio del audio transcrito
    end: float
    text: str


def clean_segments(segs: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    repeat = 0
    for s in segs:
        norm = normalize(s.text)
        if norm in _ONLY_NOISE or any(h in norm for h in _HALLUCINATIONS):
            continue
        if out and normalize(out[-1].text) == norm:
            repeat += 1
            if repeat >= 2:  # whisper en bucle repitiendo la misma frase
                continue
        else:
            repeat = 0
        out.append(Segment(s.start, s.end, s.text.strip()))
    return out


def parse_whisper_json(raw: bytes) -> list[Segment]:
    """Lee el JSON de `whisper-cli -oj`. Tolera UTF-8 cortado (pasa con emojis/tildes)."""
    text = raw.decode("utf-8", "replace")
    data = json.loads(text, strict=False)
    segs = []
    for item in data.get("transcription", []):
        off = item.get("offsets") or {}
        start = float(off.get("from", 0)) / 1000.0
        end = float(off.get("to", 0)) / 1000.0
        segs.append(Segment(start, max(end, start), str(item.get("text", "")).strip()))
    return segs


class WhisperTranscriber:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tcfg = cfg["transcripcion"]
        self._lock = threading.Lock()  # un whisper a la vez: la CPU es el cuello de botella

    def model_path(self, which: str) -> Path:
        key = "modelo_vivo" if which == "vivo" else "modelo_calidad"
        p = resolve_path(self.tcfg[key])
        if not p.exists():
            raise tools.ToolMissing(
                f"No existe el modelo de whisper {p}. Ejecuta `python -m clipmax descargar --modelo {p.stem.replace('ggml-', '')}`"
            )
        return p

    def extract_audio(self, src: str, offset: float, duration: float, wav: Path) -> None:
        tools.run([
            tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, offset):.3f}", "-t", f"{duration:.3f}", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ], timeout=max(120, duration * 2))

    def transcribe_wav(self, wav: Path, which: str = "calidad") -> list[Segment]:
        out_base = wav.with_suffix("")
        cmd = [
            tools.whisper_cli(self.cfg), "-m", str(self.model_path(which)), "-f", str(wav),
            "-l", self.tcfg["idioma"], "-t", str(int(self.tcfg["hilos"])),
            "-oj", "-of", str(out_base), "-np",
        ]
        if self.tcfg.get("prompt_inicial"):
            cmd += ["--prompt", self.tcfg["prompt_inicial"]]
        vad = self.tcfg.get("vad_modelo")
        if vad and resolve_path(vad).exists():
            cmd += ["--vad", "-vm", str(resolve_path(vad))]
        with self._lock:
            tools.run(cmd, timeout=3600)
        json_path = out_base.with_suffix(".json")
        try:
            return clean_segments(parse_whisper_json(json_path.read_bytes()))
        finally:
            json_path.unlink(missing_ok=True)

    def transcribe_window(self, src: str, offset: float, duration: float, which: str = "calidad") -> list[Segment]:
        with tempfile.TemporaryDirectory(prefix="clipmax_wh_") as tmp:
            wav = Path(tmp) / "audio.wav"
            self.extract_audio(src, offset, duration, wav)
            return self.transcribe_wav(wav, which)


def mention_signals_from_segments(db: Database, session_id: int, slug: str,
                                  segs_abs: list[tuple[float, float, str]],
                                  matcher: MentionMatcher) -> int:
    """Crea señales 'mencion_voz' cuando un streamer nombra a otro en su audio."""
    n = 0
    for start, _end, text in segs_abs:
        for target, count in matcher.find_others(text, slug, fuzzy=True).items():
            score = min(1.0, 0.6 + 0.2 * count)
            db.add_signal(session_id, slug, start, "mencion_voz", score,
                          {"target": target, "texto": text[:200]})
            n += 1
    return n


class LiveTranscriber(threading.Thread):
    """Transcribe en vivo, por tramos, a los streamers marcados con transcribir_en_vivo."""

    def __init__(self, cfg: dict, db: Database, session: dict, recorders: dict, matcher: MentionMatcher):
        super().__init__(name="live-whisper", daemon=True)
        self.cfg = cfg
        self.db = db
        self.session = session
        self.recorders = recorders
        self.matcher = matcher
        self.tr = WhisperTranscriber(cfg)
        self.chunk = float(cfg["transcripcion"]["intervalo_vivo_s"])
        self.margin = float(cfg["transcripcion"]["margen_vivo_s"])
        self.targets = [s["slug"] for s in active_streamers(cfg)
                        if s["transcribir_en_vivo"] and s["slug"] in recorders]
        self._pos: dict[int, float] = {}
        self._stop_evt = threading.Event()
        self._status: dict[str, dict] = {slug: {"retraso_s": None, "ultimo": ""} for slug in self.targets}
        self._disabled_reason = ""

    @property
    def status(self) -> dict:
        return {"objetivos": self.targets, "por_streamer": dict(self._status),
                "desactivado": self._disabled_reason}

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        if not self.targets:
            return
        try:
            tools.whisper_cli(self.cfg)
            self.tr.model_path("vivo")
        except tools.ToolMissing as exc:
            self._disabled_reason = str(exc)
            log.warning("Transcripción en vivo desactivada: %s", exc)
            return
        log.info("Transcripción en vivo activa para: %s", ", ".join(self.targets))
        while not self._stop_evt.is_set():
            worked = False
            for slug in self.targets:
                if self._stop_evt.is_set():
                    break
                worked |= self._step(slug)
            if not worked:
                self._stop_evt.wait(5)

    def _step(self, slug: str) -> bool:
        part = self.recorders[slug].current_part()
        if not part:
            return False
        pid, started = part["id"], part["started_at"]
        pos = self._pos.get(pid, 0.0)
        available = time.time() - started - self.margin
        if available - pos < self.chunk:
            return False
        if available - pos > self.chunk * 4:
            log.info("[%s] transcripción en vivo atrasada %.0fs; salto al presente", slug, available - pos)
            pos = available - self.chunk
        try:
            segs = self.tr.transcribe_window(part["path"], pos, self.chunk, "vivo")
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] fallo transcribiendo en vivo: %s", slug, exc)
            self._pos[pid] = pos + self.chunk
            return True
        abs_segs = [(started + pos + s.start, started + pos + s.end, s.text) for s in segs if s.text]
        if abs_segs:
            self.db.add_segments(self.session["id"], slug, abs_segs, "vivo")
            n = mention_signals_from_segments(self.db, self.session["id"], slug, abs_segs, self.matcher)
            if n:
                log.info("[%s] %d mención(es) por voz detectadas", slug, n)
        self._pos[pid] = pos + self.chunk
        self._status[slug] = {
            "retraso_s": round(time.time() - (started + pos + self.chunk)),
            "ultimo": abs_segs[-1][2][:140] if abs_segs else "",
        }
        return True


def transcribe_moments(cfg: dict, db: Database, session: dict, moments: list[dict],
                       progress=None) -> int:
    """Transcribe con el modelo de calidad cada ventana candidata (si no está ya transcrita)."""
    from .recorder import locate_range

    tr = WhisperTranscriber(cfg)
    offset = float(cfg["grabacion"]["desfase_chat_s"])
    done = 0
    for i, m in enumerate(moments, 1):
        if progress:
            progress(f"transcribiendo candidato {i}/{len(moments)}")
        if m.get("transcrito"):
            continue
        loc = locate_range(db, session["id"], m["slug"], m["start_ts"], m["end_ts"], offset)
        if not loc:
            log.warning("Sin video para el candidato %s (%s)", m.get("rank"), m["slug"])
            continue
        path, start, dur, wall0 = loc
        try:
            segs = tr.transcribe_window(path, start, dur, "calidad")
        except tools.ToolMissing:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Fallo transcribiendo candidato %s: %s", m.get("rank"), exc)
            continue
        abs_segs = [(wall0 + s.start, wall0 + s.end, s.text) for s in segs if s.text]
        db.delete_segments(session["id"], m["slug"], m["start_ts"] - 1, m["end_ts"] + 1, "candidato")
        db.add_segments(session["id"], m["slug"], abs_segs, "candidato")
        db.update_moment(m["id"], transcrito=1)
        done += 1
    return done
