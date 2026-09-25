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
from dataclasses import dataclass, field
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
    words: list = field(default_factory=list)  # [(inicio, fin, palabra)] relativos, si whisper los dio


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
        out.append(Segment(s.start, s.end, s.text.strip(), s.words))
    return out


def _fix(text: str) -> str:
    """Texto leído con surrogateescape -> UTF-8 correcto (whisper parte tildes entre tokens)."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _words_from_tokens(tokens: list, seg_start: float, seg_end: float) -> list[tuple[float, float, str]]:
    """Agrupa los tokens de whisper (-ojf) en palabras con sus tiempos.

    Un token que empieza con espacio abre palabra nueva; la puntuación se pega a la
    palabra anterior. Los bytes se unen antes de decodificar, así una "á" partida en
    dos tokens sale bien. Si los tiempos no son coherentes, se devuelve [] y luego se
    estiman por proporción.
    """
    dtw = _words_from_dtw(tokens, seg_start, seg_end)
    if dtw:
        return dtw
    words: list[tuple[float, float, str]] = []
    buf, w0, w1 = b"", None, None

    def flush():
        text = buf.decode("utf-8", "replace").strip()
        if text and w0 is not None:
            words.append((w0, max(w1, w0), text))

    for tok in tokens or []:
        raw = str(tok.get("text", ""))
        if raw.startswith("[_") and raw.endswith("]"):  # [_BEG_], [_TT_123]: tokens especiales
            continue
        b = raw.encode("utf-8", "surrogateescape")
        off = tok.get("offsets") or {}
        t0, t1 = float(off.get("from", 0)) / 1000.0, float(off.get("to", 0)) / 1000.0
        if b.startswith(b" ") or w0 is None:
            flush()
            buf, w0, w1 = b, t0, t1
        else:
            buf += b
            w1 = t1
    flush()
    ok = bool(words) and all(seg_start - 1.0 <= a <= b <= seg_end + 1.0 for a, b, _ in words) \
        and all(words[i][0] <= words[i + 1][0] + 0.05 for i in range(len(words) - 1)) \
        and any(b > a for a, b, _ in words)
    return words if ok else []


def _words_from_dtw(tokens: list, seg_start: float, seg_end: float) -> list[tuple[float, float, str]]:
    """Palabras con los tiempos DTW de whisper (`-dtw`), mucho más precisos que los de -ojf solo.

    Medido con voz de tiempos conocidos: los tiempos por token sin DTW se desvían de forma
    errática hasta ±1 s (subtítulos corridos); con DTW, `t_dtw` del último token de cada palabra
    cae cerca del FINAL de la palabra (mediana 0.15 s). Así que: fin = t_dtw del último token,
    inicio = fin de la palabra anterior (o una duración estimada para la primera).
    """
    groups: list[list] = []  # [bytes, t_dtw del último token]
    for tok in tokens or []:
        raw = str(tok.get("text", ""))
        if raw.startswith("[_") and raw.endswith("]"):
            continue
        t = tok.get("t_dtw")
        if t is None or float(t) < 0:
            return []
        b = raw.encode("utf-8", "surrogateescape")
        if b.startswith(b" ") or not groups:
            groups.append([b, float(t) / 100.0])
        else:
            groups[-1][0] += b
            groups[-1][1] = float(t) / 100.0
    words: list[tuple[float, float, str]] = []
    prev_end = seg_start
    for b, end in groups:
        text = b.decode("utf-8", "replace").strip()
        if not text:
            continue
        end = min(max(end, prev_end), seg_end + 0.5)
        # Empieza donde terminó la anterior, salvo que haya una pausa: entonces se usa la duración
        # típica según el largo de la palabra (si no, tras un silencio se iluminaría antes de tiempo).
        start = max(prev_end, end - min(1.0, 0.075 * len(text) + 0.2))
        start = min(start, max(prev_end, end - 0.05))
        words.append((round(start, 3), round(end, 3), text))
        prev_end = end
    return words


def parse_whisper_json(raw: bytes) -> list[Segment]:
    """Lee el JSON de `whisper-cli -oj/-ojf`. Tolera UTF-8 cortado entre tokens."""
    data = json.loads(raw.decode("utf-8", "surrogateescape"), strict=False)
    segs = []
    for item in data.get("transcription", []):
        off = item.get("offsets") or {}
        start = float(off.get("from", 0)) / 1000.0
        end = max(start, float(off.get("to", 0)) / 1000.0)
        words = _words_from_tokens(item.get("tokens"), start, end)
        segs.append(Segment(start, end, _fix(str(item.get("text", ""))).strip(), words))
    return segs


# Nombre del modelo (ggml-<x>.bin) -> preset de alineación DTW de whisper.cpp.
_DTW_PRESETS = ["large.v3.turbo", "large.v3", "large.v2", "large.v1", "medium.en", "medium",
                "small.en", "small", "base.en", "base", "tiny.en", "tiny"]


def dtw_preset(model: Path) -> str | None:
    """'ggml-small.bin' -> 'small'; 'ggml-large-v3-turbo-q5_0.bin' -> 'large.v3.turbo'."""
    name = model.stem.lower().removeprefix("ggml-").replace("-", ".").replace("_", ".")
    for p in _DTW_PRESETS:
        if name == p or name.startswith(p + "."):
            return p
    return None


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
        model = self.model_path(which)
        cmd = [
            tools.whisper_cli(self.cfg), "-m", str(model), "-f", str(wav),
            "-l", self.tcfg["idioma"], "-t", str(int(self.tcfg["hilos"])),
            "-oj", "-ojf", "-of", str(out_base), "-np",  # -ojf: tiempos por token -> subtítulos
        ]
        preset = dtw_preset(model) if which != "vivo" else None
        if preset:
            # DTW da tiempos por palabra precisos (subtítulos al compás de la voz). whisper.cpp no
            # calcula DTW con flash attention (activada por defecto), por eso -nfa.
            cmd += ["-dtw", preset, "-nfa"]
        if self.tcfg.get("prompt_inicial"):
            cmd += ["--prompt", self.tcfg["prompt_inicial"]]
        vad = self.tcfg.get("vad_modelo")
        if vad and resolve_path(vad).exists():
            cmd += ["--vad", "-vm", str(resolve_path(vad))]
        with self._lock:
            while True:
                try:
                    tools.run(cmd, timeout=3600)
                    break
                except RuntimeError as exc:
                    why = str(exc).splitlines()[-1][:120] if str(exc) else str(exc)
                    if "-dtw" in cmd:
                        # whisper-cli sin DTW (o sin -nfa): tiempos por token normales.
                        log.warning("whisper-cli no aceptó -dtw (%s); reintento sin alineación DTW", why)
                        i = cmd.index("-dtw")
                        del cmd[i:i + 2]
                        if "-nfa" in cmd:
                            cmd.remove("-nfa")
                    elif "-ojf" in cmd:
                        # whisper-cli antiguo sin tiempos por token: se transcribe igual y los
                        # subtítulos usan tiempos estimados por palabra.
                        log.warning("whisper-cli no aceptó -ojf (%s); reintento sin tiempos por palabra", why)
                        cmd.remove("-ojf")
                    else:
                        raise
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
                                  segs_abs: list[tuple],
                                  matcher: MentionMatcher) -> int:
    """Crea señales 'mencion_voz' cuando un streamer nombra a otro en su audio."""
    n = 0
    for seg in segs_abs:
        start, text = seg[0], seg[2]
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
        base = started + pos
        abs_segs = [(base + s.start, base + s.end, s.text, [[base + a, base + b, w] for a, b, w in s.words])
                    for s in segs if s.text]
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
        abs_segs = [(wall0 + s.start, wall0 + s.end, s.text, [[wall0 + a, wall0 + b, w] for a, b, w in s.words])
                    for s in segs if s.text]
        db.delete_segments(session["id"], m["slug"], m["start_ts"] - 1, m["end_ts"] + 1, "candidato")
        db.add_segments(session["id"], m["slug"], abs_segs, "candidato")
        db.update_moment(m["id"], transcrito=1)
        done += 1
    return done
