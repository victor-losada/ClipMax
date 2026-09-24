"""Efectos de edición: subtítulos dinámicos, zoom suave y efectos de sonido.

- Subtítulos: estilo TikTok, 2-4 palabras en MAYÚSCULAS con la palabra que se
  está diciendo resaltada (color + pequeño "pop"). Se generan como archivo ASS y
  ffmpeg los dibuja con libass. Los tiempos por palabra vienen de whisper
  (-ojf); si no los hay, se estiman dentro de cada frase según el largo de cada
  palabra. Como los clips tienen silencios recortados, los tiempos se remapean
  a la línea de tiempo final del clip.
- Zoom suave: acercamiento del 12 % en el remate (el "momento_clave" que marca
  Claude, o el pico de chat si no hay), con entrada y salida de 0.35 s.
- Efectos de sonido: pocos y a volumen moderado. Vienen unos generados localmente
  (sin derechos de autor); los tuyos van en la carpeta sfx/ del proyecto
  (wav/mp3/ogg, el nombre del archivo es el nombre del efecto) y Claude puede
  elegirlos por nombre.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from . import tools
from .config import PROJECT_ROOT, data_dir, resolve_path

log = logging.getLogger(__name__)

USER_SFX_DIR = PROJECT_ROOT / "sfx"
SFX_EXTS = (".wav", ".mp3", ".ogg", ".m4a", ".flac")

# Efectos generados con ffmpeg (sintéticos, sin licencias). Nombre -> fuente lavfi.
_GENERATED_SFX = {
    # "boom" grave con caída de tono (tipo vine boom) para el remate.
    "boom": "aevalsrc='0.9*sin(2*PI*(48+110*exp(-9*t))*t)*exp(-2.6*t)':d=1.4:s=48000",
    # soplido corto para las transiciones de tarjeta a clip.
    "whoosh": ("anoisesrc=d=0.55:c=pink:a=0.6:r=48000,highpass=f=350,lowpass=f=5000,"
               "afade=t=in:d=0.22,afade=t=out:st=0.25:d=0.3"),
    # campana para momentos de "¡lo dijo!".
    "ding": "aevalsrc='0.6*(sin(2*PI*1318*t)+0.45*sin(2*PI*2637*t))*exp(-4.5*t)':d=1.1:s=48000",
    # pop corto para énfasis leves.
    "pop": "aevalsrc='0.8*sin(2*PI*(900*exp(-28*t)+180)*t)*exp(-22*t)':d=0.25:s=48000",
    # golpe seco de tensión (drama, traición).
    "impacto": ("aevalsrc='0.9*sin(2*PI*(70+40*exp(-20*t))*t)*exp(-5*t)+0.35*(random(0)*2-1)*exp(-30*t)'"
                ":d=0.9:s=48000"),
}


# ---------------------------------------------------------------------------
# Efectos de sonido
# ---------------------------------------------------------------------------

def sfx_library(cfg: dict) -> dict[str, Path]:
    """{nombre: archivo}. Genera los sintéticos la primera vez; los de sfx/ los reemplazan."""
    out: dict[str, Path] = {}
    gen_dir = data_dir(cfg) / "sfx_generados"
    gen_dir.mkdir(parents=True, exist_ok=True)
    for name, src in _GENERATED_SFX.items():
        path = gen_dir / f"{name}.wav"
        if not path.exists():
            try:
                tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
                           "-af", "aformat=sample_fmts=s16:channel_layouts=stereo,alimiter=limit=0.9",
                           "-ar", "48000", str(path)], timeout=60)
            except Exception as exc:  # noqa: BLE001
                log.warning("No pude generar el efecto %s: %s", name, exc)
                continue
        out[name] = path
    if USER_SFX_DIR.exists():
        for f in sorted(USER_SFX_DIR.iterdir()):
            if f.suffix.lower() in SFX_EXTS:
                out[re.sub(r"[^a-z0-9_]+", "_", f.stem.lower()).strip("_")] = f
    return out


def sfx_names(cfg: dict) -> list[str]:
    """Nombres disponibles (para el prompt de Claude), sin generar nada."""
    names = set(_GENERATED_SFX)
    if USER_SFX_DIR.exists():
        names |= {re.sub(r"[^a-z0-9_]+", "_", f.stem.lower()).strip("_")
                  for f in USER_SFX_DIR.iterdir() if f.suffix.lower() in SFX_EXTS}
    return sorted(n for n in names if n)


# ---------------------------------------------------------------------------
# Tiempos por palabra y remapeo a la línea de tiempo del clip
# ---------------------------------------------------------------------------

def estimate_words(start: float, end: float, text: str) -> list[tuple[float, float, str]]:
    """Reparte la duración de una frase entre sus palabras según su largo."""
    words = text.split()
    if not words or end <= start:
        return []
    weights = [len(w) + 1.5 for w in words]
    total = sum(weights)
    out, t = [], start
    for w, wt in zip(words, weights):
        dt = (end - start) * wt / total
        out.append((t, t + dt, w))
        t += dt
    return out


def segment_words(segs: list[dict], origin: float, length: float) -> list[tuple[float, float, str]]:
    """Palabras de los segmentos (hora de pared) en segundos relativos a `origin`.

    Si hay segmentos 'candidato' (modelo de calidad) se usan solo esos; así no se
    duplican los subtítulos con los del transcriptor en vivo.
    """
    chosen = [s for s in segs if s.get("fuente") == "candidato"] or segs
    words: list[tuple[float, float, str]] = []
    for s in chosen:
        ws = s.get("palabras") or []
        if ws:
            items = [(a - origin, b - origin, w) for a, b, w in ws]
        else:
            items = estimate_words(s["start_ts"] - origin, s["end_ts"] - origin, s["texto"])
        words += [(max(0.0, a), min(length, b), w) for a, b, w in items if b > 0 and a < length]
    words.sort(key=lambda x: x[0])
    return words


def remap(t: float, keep: list[tuple[float, float]]) -> float | None:
    """Tiempo relativo al tramo original -> tiempo en el clip ya sin silencios (None si se cortó)."""
    acc = 0.0
    for a, b in keep:
        if a <= t <= b:
            return acc + (t - a)
        acc += b - a
    return None


def remap_words(words: list[tuple[float, float, str]], keep: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    out = []
    for a, b, w in words:
        ra = remap(a, keep)
        if ra is None:
            continue
        rb = remap(min(b, next((kb for ka, kb in keep if ka <= a <= kb), b)), keep)
        out.append((ra, max(ra + 0.08, rb if rb is not None else ra + 0.3), w))
    return out


# ---------------------------------------------------------------------------
# Subtítulos ASS
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/seguibl.ttf",
    "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Arial Black.ttf",
]


def pick_font(cfg: dict) -> tuple[Path, str, bool] | None:
    """(archivo, familia, negrita) de una fuente gruesa para los subtítulos."""
    from PIL import ImageFont

    configured = cfg["edicion"].get("subtitulos_fuente") or ""
    for c in ([configured] if configured else []) + _FONT_CANDIDATES:
        p = resolve_path(c)
        if p.exists():
            try:
                family, style = ImageFont.truetype(str(p), 20).getname()
            except OSError:
                continue
            return p, family, "bold" in (style or "").lower()
    return None


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_text(word: str) -> str:
    word = re.sub(r"[\U00010000-\U0010FFFF]", "", word)  # emojis: la fuente no los tiene
    return word.replace("\\", "").replace("{", "(").replace("}", ")").upper()


def chunk_words(words: list[tuple[float, float, str]], max_words: int = 3, max_chars: int = 18,
                max_gap: float = 0.6) -> list[list[tuple[float, float, str]]]:
    chunks: list[list[tuple[float, float, str]]] = []
    cur: list[tuple[float, float, str]] = []
    for w in words:
        if cur:
            chars = sum(len(x[2]) + 1 for x in cur) + len(w[2])
            ends_sentence = cur[-1][2][-1:] in ".?!…"
            if len(cur) >= max_words or chars > max_chars or w[0] - cur[-1][1] > max_gap or ends_sentence:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[tuple[float, float, str]], size: tuple[int, int], font_family: str,
              bold: bool, max_words: int = 3, color: str = "&H18FC53&") -> str:
    """Archivo ASS: cada palabra se ilumina mientras se dice, dentro de su grupo de 2-4 palabras."""
    w, h = size
    fs = int(min(w, h) * 0.077)
    margin_v = int(h * (0.30 if h > w else 0.27))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Sub,{font_family},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,{-1 if bold else 0},0,0,0,"
        f"100,100,1,0,1,{max(3, fs // 12)},{max(1, fs // 30)},2,{int(w * 0.06)},{int(w * 0.06)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    chunks = chunk_words(words, max_words=max_words)
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else None
        chunk_end = chunk[-1][1] + 0.25
        if next_start is not None:
            chunk_end = min(chunk_end, next_start) if next_start - chunk[-1][1] < 0.6 else chunk_end
        texts = [_ass_text(x[2]) for x in chunk]
        for i, (a, _b, _w) in enumerate(chunk):
            end = chunk[i + 1][0] if i + 1 < len(chunk) else chunk_end
            if end - a < 0.04:
                continue
            parts = []
            for j, t in enumerate(texts):
                if j == i:
                    parts.append(f"{{\\c{color}\\fscx116\\fscy116\\t(0,110,\\fscx100\\fscy100)}}{t}{{\\r}}")
                else:
                    parts.append(t)
            lines.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(end)},Sub,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


def write_subtitles(cfg: dict, words: list[tuple[float, float, str]], size: tuple[int, int],
                    work: Path, name: str) -> tuple[str, str] | None:
    """Escribe <work>/<name>.ass y copia la fuente a <work>/fonts. Devuelve (ass, fontsdir) relativos a work."""
    if not words:
        return None
    font = pick_font(cfg)
    if not font:
        log.warning("No encontré una fuente para los subtítulos; se omiten")
        return None
    path, family, bold = font
    fonts = work / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    if not (fonts / path.name).exists():
        shutil.copy2(path, fonts / path.name)
    ass = work / f"{name}.ass"
    ass.write_text(build_ass(words, size, family, bold, int(cfg["edicion"]["subtitulos_palabras"])),
                   encoding="utf-8")
    return ass.name, "fonts"


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------

def zoom_filter(src: str, dst: str, at: float, size: tuple[int, int], fps: int,
                factor: float = 1.12, hold: float = 2.2, ramp: float = 0.35) -> str:
    """Acercamiento suave centrado: entra en `ramp` s, se sostiene `hold` s y vuelve."""
    w, h = size
    t0, t1 = max(0.0, at - ramp), at + hold
    z = (f"1+{factor - 1:.3f}*clip((it-{t0:.3f})/{ramp},0,1)"
         f"*clip(({t1 + ramp:.3f}-it)/{ramp},0,1)")
    # setpts: zoompan entrega marcas de tiempo mal escaladas; sin renumerarlas, el filtro fps
    # posterior duplica fotogramas sin fin (14 s de entrada -> una hora de salida).
    return (f"[{src}]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={w}x{h}:fps={fps},setpts=N/({fps}*TB),setsar=1[{dst}]")


def auto_moment(db, session_id: int, slug: str, wall0: float, dur: float) -> float | None:
    """Si Claude no marcó el remate: pico de chat del tramo (menos ~6 s de reacción del chat)."""
    rows = db.query(
        "SELECT ts, score FROM signals WHERE session_id=? AND slug=? AND tipo IN ('pico_chat','mencion_voz') "
        "AND ts BETWEEN ? AND ? ORDER BY score DESC LIMIT 1",
        (session_id, slug, wall0, wall0 + dur + 8),
    )
    if not rows:
        return None
    t = rows[0]["ts"] - wall0 - 6.0
    return min(max(1.0, t), max(1.0, dur - 2.5))

