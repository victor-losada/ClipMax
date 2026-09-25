"""Narrador en off para el resumen de TikTok.

Motores (`edicion.narrador_motor`):
- "edge" (por defecto): voces neuronales de Microsoft (las de "Leer en voz alta" de Edge) con el
  paquete edge-tts. Gratis, sin cuenta, necesita internet. Suenan naturales y fluidas, y el
  servicio devuelve el tiempo exacto de cada palabra (subtítulos y contadores caen en la palabra).
- "piper": TTS local (sin internet). Suena más robótico; queda como respaldo automático si la
  voz de Microsoft no responde.

En los dos:
- `streamers[].pronunciacion` cambia cómo se lee un nombre (p. ej. "Güéstcol") sin tocar el texto
  de los subtítulos: se lleva la cuenta de qué letra del texto dicho viene de qué letra del escrito.
- La ficha pide cero pausas mayores de 0.3 s: los silencios largos entre frases se acortan y se
  recortan las puntas, corriendo los tiempos de las palabras.
"""

from __future__ import annotations

import array
import asyncio
import importlib
import logging
import os
import re
import subprocess
import threading
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import tools
from .config import resolve_path

log = logging.getLogger(__name__)
logging.getLogger("piper").setLevel(logging.INFO)   # en DEBUG escribe los fonemas de cada frase

EDGE_RATE = 24000
EDGE_VOZ = "es-MX-JorgeNeural"
# Voces recomendadas (las de español suenan nativas; las "multilingües" leen mejor nombres en inglés).
VOCES = [
    ("es-MX-JorgeNeural", "Jorge · México (hombre)"),
    ("es-CO-GonzaloNeural", "Gonzalo · Colombia (hombre)"),
    ("es-US-AlonsoNeural", "Alonso · EE. UU. latino (hombre)"),
    ("es-AR-TomasNeural", "Tomás · Argentina (hombre)"),
    ("es-ES-AlvaroNeural", "Álvaro · España (hombre)"),
    ("es-MX-DaliaNeural", "Dalia · México (mujer)"),
    ("es-CO-SalomeNeural", "Salomé · Colombia (mujer)"),
    ("es-US-PalomaNeural", "Paloma · EE. UU. latino (mujer)"),
    ("en-US-AndrewMultilingualNeural", "Andrew · multilingüe (hombre)"),
    ("en-US-BrianMultilingualNeural", "Brian · multilingüe (hombre)"),
    ("en-US-AvaMultilingualNeural", "Ava · multilingüe (mujer)"),
]
MAX_PAUSA = 0.28      # silencios más largos se acortan…
PAUSA = 0.16          # …a esto (entre frases)
GAP_COMA = 0.08       # Piper: hueco entre frases sintetizadas por separado
GAP_PUNTO = 0.12
_LOCK = threading.Lock()


@dataclass
class Narration:
    wav: Path
    dur: float
    words: list[tuple[float, float, str]]   # (inicio, fin, palabra tal como se escribe)


def engine(cfg: dict) -> str:
    return str(cfg["edicion"].get("narrador_motor") or "edge")


def voice_path(cfg: dict) -> Path:
    return resolve_path(cfg["edicion"]["piper_voz"])


def _has(module: str) -> bool:
    try:
        importlib.import_module(module)
    except Exception:  # noqa: BLE001 - paquete ausente o roto (p. ej. sin onnxruntime)
        return False
    return True


def piper_ready(cfg: dict) -> bool:
    return _has("piper") and voice_path(cfg).exists()


def available(cfg: dict) -> bool:
    """Hay algún narrador: la voz de Microsoft (si está edge-tts) o Piper con su voz descargada."""
    if engine(cfg) == "edge" and _has("edge_tts"):
        return True
    return piper_ready(cfg)


def describe(cfg: dict) -> str:
    if engine(cfg) == "edge" and _has("edge_tts"):
        extra = " (respaldo sin internet: Piper)" if piper_ready(cfg) else ""
        return f"voz de Microsoft {cfg['edicion'].get('narrador_voz') or EDGE_VOZ}{extra}"
    if piper_ready(cfg):
        return f"Piper {voice_path(cfg).name}"
    return "sin narrador"


def narrate(cfg: dict, text: str, out_wav: Path) -> Narration:
    """Sintetiza `text` y devuelve el WAV con los tiempos de cada palabra."""
    if engine(cfg) == "edge" and _has("edge_tts"):
        try:
            return narrate_edge(cfg, text, out_wav)
        except Exception as exc:  # noqa: BLE001
            if not piper_ready(cfg):
                raise RuntimeError(f"la voz de Microsoft no respondió: {exc}") from exc
            log.warning("La voz de Microsoft no respondió (%s); uso Piper", exc)
    return _narrate_piper(cfg, text, out_wav)


# ---------------------------------------------------------------------------
# Texto que se dice (pronunciaciones) y cómo vuelve al texto escrito
# ---------------------------------------------------------------------------
def _speakable_map(cfg: dict, text: str) -> tuple[str, list[int]]:
    """Texto para la voz y, por cada letra, la posición de la letra del texto escrito de la que viene."""
    subs: dict[str, str] = {}
    for s in cfg["streamers"]:
        say = (s.get("pronunciacion") or "").strip()
        if say:
            for name in {s["nombre"], s["slug"], *s["alias"]}:
                if len(name) > 2:
                    subs[name.lower()] = say
    out: list[str] = []
    back: list[int] = []
    pos = 0
    if subs:
        pat = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in sorted(subs, key=len, reverse=True)), re.IGNORECASE)
        for m in pat.finditer(text):
            out.extend(text[pos:m.start()])
            back.extend(range(pos, m.start()))
            say = subs[m.group(0).lower()]
            out.extend(say)
            back.extend([m.start()] * (len(say) - 1) + [m.end() - 1])
            pos = m.end()
    out.extend(text[pos:])
    back.extend(range(pos, len(text)))
    spoken = re.sub(r"[^\w\s.,;:¡!¿?\-'%$#]", " ", "".join(out))       # mismo largo: el mapa sigue sirviendo
    return spoken, back


def _speakable(cfg: dict, text: str) -> str:
    return re.sub(r"\s+", " ", _speakable_map(cfg, text)[0]).strip()


def _display_words(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", text) if re.search(r"\w", m.group(0))]


def align_words(text: str, spoken: str, back: list[int],
                bounds: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Tiempos de las palabras escritas a partir de los tiempos de lo dicho (WordBoundary).

    Cada palabra dicha se busca en el texto dicho, se lleva al texto escrito con `back` y le da su
    tiempo a la palabra escrita que la contiene. Las que no aparecen (p. ej. un número que el
    servicio devuelve distinto) se reparten entre sus vecinas."""
    low = spoken.lower()
    hits: list[tuple[int, int, float, float]] = []      # (letra inicial, final) en el texto escrito + tiempos
    cur = 0
    for a, b, t in bounds:
        t = t.strip()
        if not t:
            continue
        i = low.find(t.lower(), cur, cur + 80 + len(t))
        if i < 0:
            continue
        hits.append((back[i], back[i + len(t) - 1] + 1, a, b))
        cur = i + len(t)
    words = _display_words(text)
    times: list[list[float] | None] = []
    for s, e, _w in words:
        span = [(a, b) for hs, he, a, b in hits if hs < e and he > s]
        times.append([min(a for a, _ in span), max(b for _, b in span)] if span else None)
    # Palabras sin tiempo: reparten el hueco entre la anterior y la siguiente con tiempo.
    i = 0
    while i < len(times):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(times) and times[j] is None:
            j += 1
        lo = times[i - 1][1] if i > 0 else 0.0
        hi = times[j][0] if j < len(times) else lo + 0.35 * (j - i)
        step = max(0.0, hi - lo) / (j - i)
        for k in range(i, j):
            times[k] = [lo + (k - i) * step, lo + (k - i + 1) * step]
        i = j
    # Varias palabras con el mismo tramo (el servicio junta "3 muertes"): se reparte por letras.
    i = 0
    while i < len(times):
        j = i + 1
        while j < len(times) and times[j] == times[i]:
            j += 1
        if j - i > 1:
            a, b = times[i]
            weights = [len(re.sub(r"\W", "", words[k][2])) + 1.0 for k in range(i, j)]
            t = a
            for k, wgt in zip(range(i, j), weights):
                d = (b - a) * wgt / sum(weights)
                times[k] = [t, t + d]
                t += d
        i = j
    return [(round(t[0], 3), round(max(t[1], t[0] + 0.05), 3), w) for t, (_s, _e, w) in zip(times, words)]


# ---------------------------------------------------------------------------
# Pausas
# ---------------------------------------------------------------------------
def squeeze_pauses(samples: array.array, rate: int, max_pause: float = MAX_PAUSA,
                   keep: float = PAUSA) -> tuple[array.array, list[tuple[float, float]]]:
    """Acorta los silencios de más de `max_pause` a `keep` y recorta las puntas.

    Devuelve el audio y los tramos quitados (inicio, fin) en segundos del audio original."""
    win = max(1, rate // 100)
    env = [max((abs(v) for v in samples[i:i + win]), default=0) for i in range(0, len(samples), win)]
    if not env:
        return samples, []
    thr = max(200, int(max(env) * 0.02))
    cuts: list[tuple[int, int]] = []
    i, n = 0, len(env)
    while i < n:
        if env[i] >= thr:
            i += 1
            continue
        j = i
        while j < n and env[j] < thr:
            j += 1
        a, b = i * win, min(len(samples), j * win)
        if i == 0:
            cuts.append((0, max(0, b - int(0.02 * rate))))                 # silencio inicial
        elif j >= n:
            cuts.append((min(len(samples), a + int(0.06 * rate)), len(samples)))   # silencio final
        elif (b - a) / rate > max_pause:
            half = int(keep * rate / 2)
            cuts.append((a + half, b - half))
        i = j
    cuts = [(a, b) for a, b in cuts if b > a]
    out = array.array("h")
    pos = 0
    for a, b in cuts:
        out.extend(samples[pos:a])
        pos = b
    out.extend(samples[pos:])
    return out, [(a / rate, b / rate) for a, b in cuts]


def remap_time(t: float, cuts: list[tuple[float, float]]) -> float:
    removed = 0.0
    for a, b in cuts:
        if t >= b:
            removed += b - a
        elif t > a:
            removed += t - a
    return max(0.0, t - removed)


def _write_wav(path: Path, samples: array.array, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


# ---------------------------------------------------------------------------
# Voz de Microsoft (edge-tts)
# ---------------------------------------------------------------------------
def _rate(cfg: dict) -> str:
    """edicion.narrador_velocidad (<1 más rápido, como en Piper) -> "+5%"."""
    v = float(cfg["edicion"].get("narrador_velocidad", 0.95) or 1.0)
    pct = int(round((1.0 / max(0.5, min(2.0, v)) - 1.0) * 100))
    return f"{pct:+d}%"


async def _edge_stream(text: str, voice: str, rate: str, mp3: Path) -> list[tuple[float, float, str]]:
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary",
                                proxy=os.environ.get("HTTPS_PROXY") or None)
    bounds = []
    with open(mp3, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                a = chunk["offset"] / 1e7
                bounds.append((a, a + chunk["duration"] / 1e7, chunk["text"]))
    return bounds


def _decode(path: Path, rate: int) -> array.array:
    raw = subprocess.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
                         capture_output=True, check=True, timeout=120).stdout
    out = array.array("h")
    out.frombytes(raw[:len(raw) // 2 * 2])
    return out


def narrate_edge(cfg: dict, text: str, out_wav: Path) -> Narration:
    spoken, back = _speakable_map(cfg, text)
    voice = cfg["edicion"].get("narrador_voz") or EDGE_VOZ
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    mp3 = out_wav.with_suffix(".mp3")
    last: Exception | None = None
    for _attempt in range(3):
        try:
            bounds = asyncio.run(_edge_stream(re.sub(r"\s+", " ", spoken), voice, _rate(cfg), mp3))
            if mp3.exists() and mp3.stat().st_size > 0:
                break
            last = RuntimeError("audio vacío")
        except Exception as exc:  # noqa: BLE001 - red intermitente: se reintenta
            last = exc
    else:
        raise RuntimeError(str(last))
    # Los espacios repetidos se quitaron para la voz: el mapa se rehace sobre el texto enviado.
    sent, sent_back = [], []
    for ch, b in zip(spoken, back):
        if ch.isspace() and sent and sent[-1] == " ":
            continue
        sent.append(" " if ch.isspace() else ch)
        sent_back.append(b)
    samples, cuts = squeeze_pauses(_decode(mp3, EDGE_RATE), EDGE_RATE)
    mp3.unlink(missing_ok=True)
    words = align_words(text, "".join(sent), sent_back, bounds)
    words = [(round(remap_time(a, cuts), 3), round(remap_time(b, cuts), 3), w) for a, b, w in words]
    _write_wav(out_wav, samples, EDGE_RATE)
    return Narration(out_wav, len(samples) / EDGE_RATE, words)


# ---------------------------------------------------------------------------
# Piper (sin internet)
# ---------------------------------------------------------------------------
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


def _narrate_piper(cfg: dict, text: str, out_wav: Path) -> Narration:
    """Cada frase por separado: se conoce su inicio y fin exactos y las palabras se reparten dentro."""
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
            shown = [w for _s, _e, w in _display_words(chunk)]
            weights = [len(re.sub(r"\W", "", w)) + 1.5 for w in shown]
            total = sum(weights) or 1.0
            t = t0
            for w, k in zip(shown, weights):
                d = dur * k / total
                words.append((round(t, 3), round(t + d, 3), w))
                t += d
            audio.extend(pcm)
            audio.extend(array.array("h", [0]) * int(gap * rate))
    _write_wav(out_wav, audio, rate)
    return Narration(out_wav, len(audio) / rate, words)
