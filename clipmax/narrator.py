"""Narrador en off con Piper (TTS neuronal local y gratis) para el resumen de TikTok.

- Se sintetiza cada frase (cortando en comas y puntos) por separado: así se conoce el inicio y
  el fin exacto de cada tramo, y las palabras se reparten dentro de tramos de 1-3 s (subtítulos
  y contadores caen en la palabra).
- Se recortan los silencios de las puntas de cada tramo y se unen con huecos de 0.08-0.12 s:
  la ficha pide cero pausas mayores de 0.3 s (sin respiraciones).
- `streamers[].pronunciacion` cambia cómo se lee un nombre (p. ej. "Güéstcol") sin tocar el
  texto de los subtítulos.
"""

from __future__ import annotations

import array
import importlib
import logging
import re
import threading
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import resolve_path

log = logging.getLogger(__name__)
logging.getLogger("piper").setLevel(logging.INFO)   # en DEBUG escribe los fonemas de cada frase

GAP_COMA = 0.08
GAP_PUNTO = 0.12
_LOCK = threading.Lock()


@dataclass
class Narration:
    wav: Path
    dur: float
    words: list[tuple[float, float, str]]   # (inicio, fin, palabra tal como se escribe)


def voice_path(cfg: dict) -> Path:
    return resolve_path(cfg["edicion"]["piper_voz"])


def available(cfg: dict) -> bool:
    try:
        importlib.import_module("piper")
    except Exception:  # noqa: BLE001 - sin piper (o sin onnxruntime) no hay narrador
        return False
    return voice_path(cfg).exists()


@lru_cache(maxsize=2)
def _voice(path: str):
    from piper import PiperVoice

    return PiperVoice.load(path)


def _phrases(text: str) -> list[tuple[str, float]]:
    """Frases cortadas en puntuación, con el hueco que va después de cada una."""
    out = []
    for m in re.finditer(r"[^,.;:!?¡¿]+[,.;:!?]*", text):
        chunk = m.group(0).strip()
        if not re.search(r"\w", chunk):
            continue
        gap = GAP_PUNTO if re.search(r"[.!?;:]$", chunk) else GAP_COMA
        out.append((chunk, gap))
    return out


def _speakable(cfg: dict, text: str) -> str:
    """Texto para la voz: pronunciaciones de los streamers y sin símbolos raros."""
    for s in cfg["streamers"]:
        say = (s.get("pronunciacion") or "").strip()
        if say:
            for name in {s["nombre"], s["slug"], *s["alias"]}:
                if len(name) > 2:
                    text = re.sub(rf"\b{re.escape(name)}\b", say, text, flags=re.IGNORECASE)
    text = re.sub(r"[^\w\s.,;:¡!¿?\-'%$#áéíóúüñÁÉÍÓÚÜÑ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _trim(samples: array.array, rate: int, thr: int = 300) -> array.array:
    """Quita el silencio de las puntas (respiraciones y colas de la síntesis)."""
    win = max(1, rate // 100)
    n = len(samples)

    def loud(i: int) -> bool:
        seg = samples[i:i + win]
        return bool(seg) and max(abs(v) for v in seg) > thr
    a = 0
    while a < n and not loud(a):
        a += win
    b = n
    while b > a and not loud(max(a, b - win)):
        b -= win
    pad = rate // 50                          # 20 ms de margen para no comerse consonantes
    return samples[max(0, a - pad):min(n, b + pad)]


def _split_words(chunk: str) -> list[str]:
    return [w for w in re.split(r"\s+", chunk.strip()) if re.search(r"\w", w)]


def narrate(cfg: dict, text: str, out_wav: Path) -> Narration:
    """Sintetiza `text` y devuelve el WAV con los tiempos de cada palabra."""
    from piper import SynthesisConfig

    voice = _voice(str(voice_path(cfg)))
    rate = int(voice.config.sample_rate)
    syn = SynthesisConfig(length_scale=float(cfg["edicion"].get("narrador_velocidad", 0.95)))
    audio = array.array("h")
    words: list[tuple[float, float, str]] = []
    with _LOCK:
        for chunk, gap in _phrases(text):
            say = _speakable(cfg, chunk)
            if not say:
                continue
            pcm = array.array("h")
            for piece in voice.synthesize(say, syn_config=syn):
                pcm.frombytes(piece.audio_int16_bytes)
            pcm = _trim(pcm, rate)
            if not pcm:
                continue
            t0 = len(audio) / rate
            dur = len(pcm) / rate
            shown = _split_words(chunk)
            weights = [len(re.sub(r"\W", "", w)) + 1.5 for w in shown]
            total = sum(weights) or 1.0
            t = t0
            for w, k in zip(shown, weights):
                d = dur * k / total
                words.append((round(t, 3), round(t + d, 3), w))
                t += d
            audio.extend(pcm)
            audio.extend(array.array("h", [0]) * int(gap * rate))
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(audio.tobytes())
    return Narration(out_wav, len(audio) / rate, words)
