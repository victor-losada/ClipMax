"""Edición automática con ffmpeg.

Por cada elemento del guion de Claude:
- narración -> tarjeta (fondo desenfocado del clip siguiente + texto, y voz si
  está activada);
- clip -> se ubica el tramo exacto en el MP4 del streamer, se recortan los
  silencios largos (huecos sin voz según la transcripción) y se une todo en una
  sola pasada de ffmpeg: recorte de cámara según el modo del streamer, fondo
  desenfocado si cambia la relación de aspecto, título en pantalla, fundidos de
  audio de 40 ms en cada corte (sin "pops") y normalización de volumen, para
  que todos los streamers suenen parejo.
Todas las piezas se codifican con parámetros idénticos y se concatenan sin
recodificar al final.
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import effects, style, tools
from .cards import render_card, render_lower_third, render_split_tags
from .config import get_streamer, session_dir, streamer_name
from .db import Database
from .recorder import locate_range, snapshot_from_file
from .tts import synthesize

log = logging.getLogger(__name__)


@dataclass
class ClipSpec:
    slug: str
    nombre: str
    path: str
    file_start: float        # segundo del archivo donde empieza el tramo
    dur: float
    wall0: float             # hora de pared del inicio del tramo
    titulo: str = ""
    keep: list[tuple[float, float]] = field(default_factory=list)  # relativo a file_start
    words: list[tuple[float, float, str]] = field(default_factory=list)  # palabras, relativas al tramo
    momento: float | None = None     # remate (relativo al tramo): zoom + efecto de sonido
    efecto: str = ""                 # efecto de sonido en el remate
    whoosh: bool = False             # efecto de transición al entrar (viene de una tarjeta)
    partner: "ClipSpec | None" = None  # otro streamer a la par (pantalla dividida)
    # Estilo "Eufonía" (clipmax/style.py): plan de zooms/cara/captions, bloque y audio del tramo.
    plan: object | None = None
    bloque: str = ""
    title_hold: float | None = None  # segundos que se queda el PNG de título/rótulo (None = 5 s con fundido)
    loud_target: float | None = None  # LUFS del tramo según el bloque (None = -16 clásico)
    audio_gain: float = 1.0           # <1 baja la voz (sting)

    @property
    def kept_duration(self) -> float:
        return sum(b - a for a, b in self.keep) if self.keep else self.dur


# ---------------------------------------------------------------------------
# Silencios
# ---------------------------------------------------------------------------

def merge_intervals(iv: list[tuple[float, float]], max_gap: float = 0.0) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if out and a - out[-1][1] <= max_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def speech_keep_intervals(clip_len: float, speech: list[tuple[float, float]], max_gap: float,
                          pad: float = 0.35, tail: float = 1.5, min_piece: float = 0.6,
                          min_ratio: float = 0.25, min_keep_s: float = 4.0) -> list[tuple[float, float]]:
    """Qué conservar de un clip dado dónde hay voz: se eliminan huecos sin voz > max_gap.

    - Cada tramo de voz se amplía `pad` s a cada lado (whisper no es exacto al milisegundo).
    - Tras la última frase se deja `tail` s para la reacción (risa, grito).
    - Huecos <= max_gap se conservan: son pausas naturales del habla.
    - Si recortar dejaría menos del 25 % del clip (o menos de 4 s), no se recorta: es un
      momento visual o de reacción sin palabras, o la transcripción no lo cubre.
    """
    if not speech:
        return [(0.0, clip_len)]
    padded = [(max(0.0, a - pad), min(clip_len, b + pad)) for a, b in speech if b > a]
    if not padded:
        return [(0.0, clip_len)]
    merged = merge_intervals(padded, max_gap)
    last_a, last_b = merged[-1]
    merged[-1] = (last_a, min(clip_len, last_b + tail))
    keep = [(round(a, 3), round(b, 3)) for a, b in merged if b - a >= min_piece]
    kept = sum(b - a for a, b in keep)
    if not keep or kept < min(min_keep_s, clip_len) or kept < clip_len * min_ratio:
        return [(0.0, clip_len)]
    return keep


_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def audio_keep_intervals(path: str, start: float, dur: float, noise_db: float, min_sil: float,
                         pad: float = 0.25) -> list[tuple[float, float]]:
    """Alternativa sin transcripción: silencedetect de ffmpeg sobre el audio."""
    proc = tools.run([
        tools.ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", path,
        "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-",
    ], timeout=600, check=False)
    err = (proc.stderr or b"").decode("utf-8", "replace")
    starts = [float(x) for x in _SIL_START.findall(err)]
    ends = [float(x) for x in _SIL_END.findall(err)]
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else dur
        silences.append((max(0.0, s + pad), min(dur, e - pad)))
    keep, cur = [], 0.0
    for s, e in silences:
        if e - s <= 0:
            continue
        if s > cur:
            keep.append((cur, s))
        cur = max(cur, e)
    if cur < dur:
        keep.append((cur, dur))
    return [(round(a, 3), round(b, 3)) for a, b in keep if b - a >= 0.5] or [(0.0, dur)]


def snap_to_segments(a: float, b: float, segs: list, cand_dur: float) -> tuple[float, float]:
    """Ajusta los cortes de Claude a límites de frase para no cortar palabras a la mitad."""
    for s0, s1, _t in segs:
        if s0 < a < s1 and a - s0 < 2.0:
            a = s0 - 0.25
        if s0 < b < s1 and s1 - b < 3.0:
            b = s1 + 0.3
    return max(0.0, a), min(cand_dur, b)


# ---------------------------------------------------------------------------
# Filtros de video
# ---------------------------------------------------------------------------

def _even(x: float) -> int:
    """Tamaño par (yuv420p lo exige), mínimo 2."""
    return max(2, int(x) // 2 * 2)


def _even_off(x: float) -> int:
    """Desplazamiento par; 0 sigue siendo 0."""
    return max(0, int(x) // 2 * 2)


def fit_blur(src: str, dst: str, iw: int, ih: int, w: int, h: int, tag: str) -> str:
    """Encaja [src] en w×h; si la relación de aspecto difiere, rellena con el mismo video desenfocado."""
    if abs(iw / ih - w / h) < 0.01:
        return f"[{src}]scale={w}:{h}:flags=bicubic,setsar=1[{dst}]"
    bw, bh = _even(w / 8), _even(h / 8)
    return (
        f"[{src}]split=2[{tag}b][{tag}f];"
        f"[{tag}b]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur=6:2,scale={w}:{h},eq=brightness=-0.12[{tag}bo];"
        f"[{tag}f]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bicubic,"
        f"setsar=1[{tag}fo];"
        f"[{tag}bo][{tag}fo]overlay=(W-w)/2:(H-h)/2,setsar=1[{dst}]"
    )


def layout_filter(src: str, dst: str, src_size: tuple[int, int], out_size: tuple[int, int],
                  modo: str, camara: dict | None) -> str:
    """Encuadre según el modo del streamer y el formato de salida.

    - juego_cara + horizontal: el stream completo.
    - cara: solo el recuadro de la cámara (config streamers[].camara), ampliado.
    - juego_cara + vertical con cámara definida: cámara arriba, juego abajo (formato TikTok).
    """
    iw, ih = src_size
    w, h = out_size
    cam = None
    if camara:
        cw, ch = _even(camara["w"] * iw), _even(camara["h"] * ih)
        cx, cy = _even_off(camara["x"] * iw), _even_off(camara["y"] * ih)
        cw, ch = min(cw, iw - cx), min(ch, ih - cy)
        cam = (cw, ch, cx, cy)
    if modo == "cara" and cam:
        cw, ch, cx, cy = cam
        return f"[{src}]crop={cw}:{ch}:{cx}:{cy}[lcam];" + fit_blur("lcam", dst, cw, ch, w, h, "lc")
    if h > w and cam:
        cw, ch, cx, cy = cam
        top = _even(h * 0.38)
        return (
            f"[{src}]split=2[lsc][lsg];"
            f"[lsc]crop={cw}:{ch}:{cx}:{cy},scale={w}:{top}:force_original_aspect_ratio=increase,"
            f"crop={w}:{top},setsar=1[ltop];"
            + fit_blur("lsg", "lbot", iw, ih, w, h - top, "lg") + ";"
            f"[ltop][lbot]vstack=inputs=2[{dst}]"
        )
    return fit_blur(src, dst, iw, ih, w, h, "lf")


def encode_args(cfg: dict) -> list[str]:
    ed = cfg["edicion"]
    codec = ed["codec"]
    fps = int(ed["fps"])
    args = ["-c:v", codec]
    if codec == "libx264":
        args += ["-preset", ed["preset"], "-crf", str(ed["crf"])]
    elif "nvenc" in codec:
        args += ["-preset", "p5", "-rc", "vbr", "-cq", str(ed["crf"]), "-b:v", "0"]
    elif "qsv" in codec:
        args += ["-global_quality", str(ed["crf"])]
    elif "amf" in codec:
        args += ["-rc", "cqp", "-qp_i", str(ed["crf"]), "-qp_p", str(ed["crf"])]
    args += ["-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
             "-video_track_timescale", "90000",
             "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]
    return args


# ---------------------------------------------------------------------------
# Piezas
# ---------------------------------------------------------------------------

_size_cache: dict[str, tuple[int, int]] = {}


def _src_size(path: str) -> tuple[int, int]:
    if path not in _size_cache:
        _size_cache[path] = tools.probe_video_size(path)
    return _size_cache[path]


def prepare_clip(cfg: dict, db: Database, session: dict, cand: dict, inicio: float, fin: float,
                 titulo: str = "", *, max_gap: float | None = None, keep_all: bool = False) -> ClipSpec | None:
    """Ubica el tramo en disco y calcula qué partes conservar (sin silencios largos).

    max_gap: silencio máximo (por defecto edicion.silencio_max_s); keep_all: no recortar nada
    (pausas dramáticas, el gancho)."""
    inicio, fin = snap_to_segments(inicio, fin, cand.get("transcripcion", []), cand["duracion"])
    t0, t1 = cand["start_ts"] + inicio, cand["start_ts"] + fin
    loc = locate_range(db, session["id"], cand["slug"], t0, t1, float(cfg["grabacion"]["desfase_chat_s"]))
    if not loc:
        log.warning("No hay video para el candidato %s (%s)", cand["id"], cand["slug"])
        return None
    path, start, dur, wall0 = loc
    spec = ClipSpec(slug=cand["slug"], nombre=streamer_name(cfg, cand["slug"]), path=path,
                    file_start=start, dur=dur, wall0=wall0, titulo=titulo)
    ed = cfg["edicion"]
    method = "ninguno" if keep_all else ed["metodo_silencios"]
    max_gap = float(ed["silencio_max_s"]) if max_gap is None else max_gap
    segs = db.segments(session["id"], cand["slug"], wall0, wall0 + dur)
    spec.words = effects.segment_words(segs, wall0, dur)
    if method == "transcripcion":
        speech = [(max(0.0, s["start_ts"] - wall0), min(dur, s["end_ts"] - wall0)) for s in segs]
        spec.keep = speech_keep_intervals(dur, speech, max_gap)
    elif method == "audio":
        spec.keep = audio_keep_intervals(path, start, dur, float(ed["umbral_silencio_db"]), max_gap)
    else:
        spec.keep = [(0.0, dur)]
    return spec


def prepare_partner(cfg: dict, db: Database, session: dict, main: ClipSpec, cand: dict) -> ClipSpec | None:
    """El mismo tramo de tiempo (hora de pared) en el stream de otro streamer."""
    if cand["slug"] == main.slug:
        return None
    loc = locate_range(db, session["id"], cand["slug"], main.wall0, main.wall0 + main.dur,
                       float(cfg["grabacion"]["desfase_chat_s"]))
    if not loc or loc[2] < main.dur * 0.5:
        return None
    path, start, dur, wall0 = loc
    return ClipSpec(slug=cand["slug"], nombre=streamer_name(cfg, cand["slug"]), path=path,
                    file_start=start, dur=dur, wall0=wall0)


def _half_filter(src: str, dst: str, src_size: tuple[int, int], half: tuple[int, int],
                 streamer: dict, tag: str) -> str:
    """Una mitad de la pantalla dividida: la cámara si está definida (caras), si no el stream completo."""
    iw, ih = src_size
    hw, hh = half
    cam = streamer.get("camara")
    if cam:
        cw, ch = _even(cam["w"] * iw), _even(cam["h"] * ih)
        cx, cy = _even_off(cam["x"] * iw), _even_off(cam["y"] * ih)
        cw, ch = min(cw, iw - cx), min(ch, ih - cy)
        return (f"[{src}]crop={cw}:{ch}:{cx}:{cy},scale={hw}:{hh}:force_original_aspect_ratio=increase,"
                f"crop={hw}:{hh},setsar=1[{dst}]")
    return fit_blur(src, dst, iw, ih, hw, hh, tag)


def split_layout(main_src: str, partner_src: str, dst: str, main: tuple[tuple[int, int], dict],
                 partner: tuple[tuple[int, int], dict], out_size: tuple[int, int]) -> str:
    """Pantalla dividida: lado a lado en horizontal, arriba/abajo en vertical."""
    w, h = out_size
    vertical = h > w
    half = (w, _even(h / 2)) if vertical else (_even(w / 2), h)
    a = _half_filter(main_src, "hmo", main[0], half, main[1], "hm")
    b = _half_filter(partner_src, "hpo", partner[0], half, partner[1], "hp")
    stack = "vstack" if vertical else "hstack"
    return f"{a};{b};[hmo][hpo]{stack}=inputs=2,scale={w}:{h},setsar=1[{dst}]"


def _remap_near(t: float | None, keep: list[tuple[float, float]]) -> float | None:
    """Como effects.remap, pero si el instante cayó en un silencio cortado usa el siguiente tramo."""
    if t is None:
        return None
    r = effects.remap(t, keep)
    if r is not None:
        return r
    for a, _b in keep:
        if a > t:
            return effects.remap(a, keep)
    return None


def render_clip(cfg: dict, spec: ClipSpec, out: Path, out_size: tuple[int, int],
                title_png: Path | None = None, *, work: Path | None = None,
                sfx_lib: dict | None = None, effects_on: bool = True) -> float:
    """Renderiza un clip. Si algún efecto hace fallar a ffmpeg, reintenta sin efectos."""
    try:
        return _render_clip(cfg, spec, out, out_size, title_png, work, sfx_lib, effects_on)
    except Exception as exc:  # noqa: BLE001
        if not effects_on:
            raise
        log.warning("[%s] el render con efectos falló; reintento sin efectos: %s", spec.slug, str(exc)[-400:])
        return _render_clip(cfg, spec, out, out_size, title_png, work, None, False)


# Audio alineado al cero del corte (ver _render_clip): recorta lo negativo y rellena con silencio.
SYNC_AUDIO = "aresample=async=1:first_pts=0"


def audio_shift(cfg: dict) -> str:
    """Filtro extra para edicion.desfase_audio_s: + retrasa la voz, - la adelanta."""
    shift = float(cfg["edicion"].get("desfase_audio_s") or 0.0)
    if shift > 0:
        return f",adelay=delays={int(round(shift * 1000))}:all=1"
    if shift < 0:
        return f",atrim=start={-shift:.3f},asetpts=PTS-STARTPTS"
    return ""


def _render_clip(cfg: dict, spec: ClipSpec, out: Path, out_size: tuple[int, int], title_png: Path | None,
                 work: Path | None, sfx_lib: dict | None, effects_on: bool) -> float:
    ed = cfg["edicion"]
    fps = int(ed["fps"])
    streamer = get_streamer(cfg, spec.slug) or {"modo": "juego_cara", "camara": None}
    modo = streamer.get("modo", "juego_cara")
    if modo == "cara" and not streamer.get("camara"):
        log.warning("[%s] modo 'cara' sin recuadro de cámara configurado; uso el stream completo", spec.slug)
    kept = spec.kept_duration
    keep = spec.keep or [(0.0, spec.dur)]
    k = len(keep)
    # Si se adelanta el audio (desfase negativo) hace falta leer ese poco más de la fuente.
    extra = max(0.0, -float(ed.get("desfase_audio_s") or 0.0))
    inputs: list[list[str]] = [["-ss", f"{spec.file_start:.3f}", "-t", f"{spec.dur + extra:.3f}", "-i", spec.path]]

    def add_input(args: list[str]) -> int:
        inputs.append(args)
        return len(inputs) - 1

    partner = spec.partner if (effects_on and ed.get("pantalla_dividida")) else None
    # Sincronía: tras -ss, audio y video comparten el mismo cero (el punto de corte), pero el video
    # puede empezar antes (keyframe anterior, marcas negativas) o después (el siguiente keyframe:
    # Kick pone uno cada ~2 s). Restar el inicio de cada pista por separado (PTS-STARTPTS) los
    # desfasaba hasta 2 s; en cambio se alinean las dos al cero común: fps/aresample con
    # start_time/first_pts=0 recortan lo negativo y rellenan el hueco inicial.
    # fps además baja los 60 fps de Kick a los de salida antes del zoom (si no, cámara lenta).
    parts = [f"[0:v]fps={fps}:start_time=0,split={k}" + "".join(f"[vs{i}]" for i in range(k)),
             f"[0:a]{SYNC_AUDIO}{audio_shift(cfg)},asplit={k}" + "".join(f"[as{i}]" for i in range(k))]
    for i, (a, b) in enumerate(keep):
        ln = b - a
        fade = min(0.04, ln / 4)
        parts.append(f"[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0.0, ln - fade):.3f}:d={fade:.3f}[a{i}]")
    parts.append("".join(f"[v{i}][a{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[vc][ac]")

    if partner:
        ip = add_input(["-ss", f"{partner.file_start:.3f}", "-t", f"{partner.dur:.3f}", "-i", partner.path])
        pre = max(0.0, partner.wall0 - spec.wall0)
        post = max(0.0, spec.dur - pre - partner.dur) + 1.0
        # Solo imagen del otro streamer: mezclar los dos audios haría eco (suelen estar en la misma llamada).
        parts.append(f"[{ip}:v]fps={fps}:start_time=0,tpad=start_mode=clone:start_duration={pre:.3f}:"
                     f"stop_mode=clone:stop_duration={post:.3f},split={k}" + "".join(f"[ps{i}]" for i in range(k)))
        for i, (a, b) in enumerate(keep):
            parts.append(f"[ps{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[p{i}]")
        parts.append("".join(f"[p{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=0[vcp]")
        pst = get_streamer(cfg, partner.slug) or {"camara": None}
        parts.append(split_layout("vc", "vcp", "vl", (_src_size(spec.path), streamer),
                                  (_src_size(partner.path), pst), out_size))
    else:
        plan = spec.plan if effects_on else None
        src = "vc"
        if plan is not None and plan.facecam and plan.facecam_px:
            # La cara a pantalla completa sale de la imagen original (rama aparte, antes del encuadre).
            parts.append("[vc]split=2[vcl][vcf]")
            src = "vcl"
            cw, ch, cx, cy = plan.facecam_px
            w_out, h_out = out_size
            ratio = (cw / ch) / (w_out / h_out)
            if 1 / 1.35 <= ratio <= 1.35:   # proporción parecida: llena el cuadro
                parts.append(f"[vcf]crop={cw}:{ch}:{cx}:{cy},scale={w_out}:{h_out}:force_original_aspect_ratio="
                             f"increase,crop={w_out}:{h_out},setsar=1[vfcs]")
            else:                           # muy distinta: la cámara entera con fondo desenfocado
                parts.append(f"[vcf]crop={cw}:{ch}:{cx}:{cy}[vfcc]")
                parts.append(fit_blur("vfcc", "vfcs", cw, ch, w_out, h_out, "fcb"))
        parts.append(layout_filter(src, "vl", _src_size(spec.path), out_size, modo, streamer.get("camara")))
    cur = "vl"

    moment = _remap_near(spec.momento, keep) if effects_on else None
    plan = spec.plan if (effects_on and not partner) else None
    if plan is not None:
        # Plan del director (style.py): cara a pantalla completa, zoom a textos y punch-ins al facecam.
        if plan.facecam and plan.facecam_px:
            enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in plan.facecam)
            parts.append(f"[{cur}][vfcs]overlay=0:0:enable='{enable}'[vfc]")
            cur = "vfc"
        if plan.zooms and ed.get("zoom", True):
            parts.append(effects.zoom_windows_filter(cur, "vz", plan.zooms, out_size, fps))
            cur = "vz"
    elif moment is not None and ed.get("zoom") and not partner:
        parts.append(effects.zoom_filter(cur, "vz", moment, out_size, fps, float(ed.get("zoom_factor", 1.12))))
        cur = "vz"
    if title_png:
        hold = spec.title_hold
        title_dur = min(hold if hold else 5.0, kept)
        it = add_input(["-loop", "1", "-framerate", str(fps), "-t", f"{title_dur:.2f}", "-i", str(title_png)])
        fade_out = "" if hold and hold >= kept else \
            f",fade=t=out:st={max(0.5, title_dur - 0.5):.2f}:d=0.4:alpha=1"
        parts.append(f"[{it}:v]format=rgba,fade=t=in:st=0.2:d=0.3:alpha=1{fade_out}[ttl]")
        parts.append(f"[{cur}][ttl]overlay=0:0:eof_action=pass[vt]")
        cur = "vt"
    if partner and work:
        tags = render_split_tags(spec.nombre, partner.nombre, out_size, work / f"{out.stem}_tags.png",
                                 cfg["edicion"]["fuente"])
        itg = add_input(["-loop", "1", "-framerate", str(fps), "-t", f"{kept:.2f}", "-i", str(tags)])
        parts.append(f"[{cur}][{itg}:v]overlay=0:0:eof_action=pass[vtag]")
        cur = "vtag"
    cwd = None
    if effects_on and work and plan is not None and plan.captions is not None:
        # Estilo Eufonía: captions de frases clave (1-4 palabras, color del streamer).
        from . import style

        sub = style.write_key_captions(plan, out_size, work, out.stem)
        if sub:
            parts.append(f"[{cur}]subtitles=f={sub[0]}:fontsdir={sub[1]}[vsub]")
            cur, cwd = "vsub", work
    elif effects_on and ed.get("subtitulos") and work:
        sub = effects.write_subtitles(cfg, effects.remap_words(spec.words, keep), out_size, work, out.stem)
        if sub:
            parts.append(f"[{cur}]subtitles=f={sub[0]}:fontsdir={sub[1]}[vsub]")
            cur, cwd = "vsub", work
    parts.append(f"[{cur}]fps={fps},format=yuv420p[vout]")

    # Volumen: -16 LUFS parejo (clásico) o el del bloque con más rango (Eufonía: la energía crece).
    target = spec.loud_target if spec.loud_target is not None else -16.0
    lra = 15 if spec.loud_target is not None else 11
    gain = f",volume={spec.audio_gain:.3f}" if spec.audio_gain != 1.0 else ""
    parts.append(f"[ac]aresample=48000,loudnorm=I={target:.1f}:TP=-1.5:LRA={lra},aresample=48000{gain},"
                 "aformat=sample_fmts=fltp:channel_layouts=stereo[asp]")
    events: list[tuple[Path, float, float]] = []
    if effects_on and ed.get("efectos_sonido") and sfx_lib:
        vol = float(ed.get("sfx_volumen", 0.55))
        trans = ed.get("sfx_transicion") or ""
        if spec.whoosh and trans in sfx_lib:
            events.append((sfx_lib[trans], 0.0, vol * 0.6))
        if spec.efecto and spec.efecto in sfx_lib and moment is not None:
            lead = 0.3 if (ed.get("zoom") and not partner) else 0.0  # que el golpe caiga con el zoom
            events.append((sfx_lib[spec.efecto], max(0.0, moment - lead), vol))
    if events:
        labels = []
        for j, (path, t, v) in enumerate(events):
            isx = add_input(["-i", str(path)])
            parts.append(f"[{isx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"adelay=delays={int(t * 1000)}:all=1,volume={v:.2f}[sfx{j}]")
            labels.append(f"[sfx{j}]")
        parts.append("[asp]" + "".join(labels) + f"amix=inputs={1 + len(labels)}:duration=first:"
                     "dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]")
    else:
        parts.append("[asp]anull[aout]")

    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for args in inputs:
        cmd += args
    # -t en la salida: tope de seguridad por si algún filtro se desboca.
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]", *encode_args(cfg),
            "-t", f"{kept + 0.5:.2f}", str(out)]
    tools.run(cmd, timeout=max(240, spec.dur * 12), cwd=cwd)
    return kept


def _reading_time(text: str) -> float:
    return min(10.0, max(3.0, len(text.split()) / 2.6 + 1.2))


def render_card_piece(cfg: dict, text: str, out: Path, out_size: tuple[int, int], *,
                      bg: Path | None = None, label: str = "", big: bool = False,
                      voice: bool = True, png: Path | None = None, dur: float | None = None,
                      eufonia: bool = False) -> float:
    """Pieza fija (tarjeta): la de narración, o un PNG ya hecho (`png`, p. ej. la pantalla final)."""
    w, h = out_size
    fps = int(cfg["edicion"]["fps"])
    if png is None:
        png = out.with_suffix(".png")
        if eufonia:
            from .cards import render_card_eufonia
            render_card_eufonia(text, out_size, png, bg_image=bg, label=label)
        else:
            render_card(text, out_size, png, bg_image=bg, label=label, font_path=cfg["edicion"]["fuente"], big=big)
    wav = out.with_suffix(".wav")
    has_voice = voice and bool(text) and synthesize(cfg, text, wav)
    dur = dur or _reading_time(text)
    if has_voice:
        dur = max(2.5, tools.probe_duration(wav) + 0.7)
    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.2f}", "-i", str(png)]
    if has_voice:
        cmd += ["-i", str(wav)]
        audio = ("[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"adelay=200:all=1,apad,atrim=0:{dur:.2f}[a]")
    else:
        cmd += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio = f"[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo,atrim=0:{dur:.2f}[a]"
    graph = (f"[0:v]scale={w}:{h},setsar=1,fade=t=in:st=0:d=0.25,"
             f"fade=t=out:st={max(0.0, dur - 0.3):.2f}:d=0.3,fps={fps},format=yuv420p[v];" + audio)
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", *encode_args(cfg), "-t", f"{dur:.2f}", str(out)]
    tools.run(cmd, timeout=300)
    return dur


def concat_pieces(pieces: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(tools.concat_list_line(p) for p in pieces), encoding="utf-8")
    try:
        tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                   "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)], timeout=3600)
    finally:
        lst.unlink(missing_ok=True)
    return out


def output_size(cfg: dict, vertical: bool | None = None) -> tuple[int, int]:
    ed = cfg["edicion"]
    w, h = int(ed["ancho"]), int(ed["alto"])
    if vertical is True and w > h:
        return 1080, 1920
    return w, h


def slugify(text: str, maxlen: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen] or "clip"


# ---------------------------------------------------------------------------
# Render completo
# ---------------------------------------------------------------------------

def apply_clip_effects(cfg: dict, db: Database, session: dict, spec: ClipSpec, cand: dict,
                       item: dict, by_id: dict[int, dict]) -> None:
    """Traduce lo que marcó Claude (remate, efecto, pantalla dividida) al tramo del clip."""
    ed = cfg["edicion"]
    mom = float(item.get("momento_clave") or 0)
    if mom > 0:
        rel = cand["start_ts"] + mom - spec.wall0
        spec.momento = rel if 0 <= rel <= spec.dur else None
    if spec.momento is None and ed.get("zoom_auto"):
        spec.momento = effects.auto_moment(db, session["id"], spec.slug, spec.wall0, spec.dur)
    spec.efecto = str(item.get("efecto_sonido") or "")
    other = by_id.get(int(item.get("pantalla_dividida_con") or 0))
    if other and ed.get("pantalla_dividida"):
        spec.partner = prepare_partner(cfg, db, session, spec, other)


def budget_sfx(cfg: dict, specs: list[ClipSpec]) -> None:
    """'Uno que otro' efecto: los de Claude primero y luego transiciones hasta el máximo del video."""
    limit = int(cfg["edicion"].get("sfx_max_por_video", 10))
    used = 0
    for s in specs:
        if s.efecto:
            if used < limit:
                used += 1
            else:
                s.efecto = ""
    for s in specs:
        if s.whoosh:
            if used < limit:
                used += 1
            else:
                s.whoosh = False


def render_summary(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                   progress=None) -> Path:
    if cfg["edicion"].get("estilo", "eufonia") == "eufonia":
        from .montage import render_summary_eufonia

        return render_summary_eufonia(cfg, db, session, decision, candidates, progress)
    fecha = session["fecha"]
    work = session_dir(cfg, fecha) / "render"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    size = output_size(cfg)
    by_id = {int(c["id"]): c for c in candidates}
    label = cfg["evento"]["nombre"]

    # 1) Resolver todos los clips primero (para saber qué fondo usar en cada tarjeta).
    items = decision["guion"]
    specs: dict[int, ClipSpec] = {}
    for idx, it in enumerate(items):
        if it["tipo"] == "clip":
            cand = by_id.get(int(it["candidato_id"]))
            if cand:
                spec = prepare_clip(cfg, db, session, cand, it["inicio"], it["fin"], it.get("titulo_en_pantalla", ""))
                if spec:
                    apply_clip_effects(cfg, db, session, spec, cand, it, by_id)
                    spec.whoosh = idx > 0 and items[idx - 1]["tipo"] == "narracion"
                    specs[idx] = spec
    budget_sfx(cfg, [specs[i] for i in sorted(specs)])
    sfx_lib = effects.sfx_library(cfg) if cfg["edicion"].get("efectos_sonido") else None

    def bg_for(idx: int) -> Path | None:
        """Fotograma del clip siguiente (o anterior) como fondo de la tarjeta."""
        order = [j for j in range(idx + 1, len(items))] + [j for j in range(idx - 1, -1, -1)]
        for j in order:
            if j in specs:
                png = work / f"bg_{j:03d}.jpg"
                if not png.exists():
                    try:
                        snapshot_from_file(specs[j].path, specs[j].file_start + 1.0, png, width=size[0])
                    except Exception:  # noqa: BLE001
                        return None
                return png
        return None

    pieces: list[Path] = []
    total = 0.0
    n_items = len(items) + 1
    # 2) Tarjeta de apertura con el título del video.
    if progress:
        progress(f"render 1/{n_items}: portada")
    opening = work / "000_portada.mp4"
    total += render_card_piece(cfg, f"{decision['titulo_video']}\n{fecha}", opening, size,
                               bg=bg_for(-1), label=label, big=True, voice=False)
    pieces.append(opening)

    for idx, it in enumerate(items):
        if progress:
            progress(f"render {idx + 2}/{n_items}: {it['tipo']}")
        out = work / f"{idx + 1:03d}_{it['tipo']}.mp4"
        try:
            if it["tipo"] == "narracion":
                total += render_card_piece(cfg, it["texto"], out, size, bg=bg_for(idx), label=label)
            else:
                spec = specs.get(idx)
                if not spec:
                    continue
                title_png = None
                if cfg["edicion"]["titulos_en_pantalla"] and (spec.titulo or spec.nombre):
                    title_png = render_lower_third(spec.titulo, spec.nombre, size, work / f"t_{idx:03d}.png",
                                                   cfg["edicion"]["fuente"])
                total += render_clip(cfg, spec, out, size, title_png, work=work, sfx_lib=sfx_lib)
        except Exception as exc:  # noqa: BLE001 - una pieza rota no debe tumbar el video entero
            log.error("Falló la pieza %d (%s): %s", idx, it["tipo"], exc)
            continue
        pieces.append(out)

    if len(pieces) <= 1:
        raise RuntimeError("No se pudo renderizar ningún clip; revisa el log")
    final = session_dir(cfg, fecha) / f"resumen_{fecha}.mp4"
    if progress:
        progress("uniendo piezas")
    concat_pieces(pieces, final)
    real = tools.probe_duration(final)
    ed = cfg["edicion"]
    if real < float(ed["duracion_min_min"]) * 60:
        log.warning("El resumen dura %.1f min (< %s min objetivo)", real / 60, ed["duracion_min_min"])
    log.info("Resumen listo: %s (%.1f min)", final.name, real / 60)
    db.add_output(session["id"], "video", str(final), {"duracion_s": round(real, 1), "piezas": len(pieces)})
    if not log.isEnabledFor(logging.DEBUG):
        shutil.rmtree(work, ignore_errors=True)
    return final


def trim_keep(keep: list[tuple[float, float]], max_len: float) -> list[tuple[float, float]]:
    """Recorta los tramos a conservar para que sumen como mucho max_len segundos."""
    out, used = [], 0.0
    for a, b in keep:
        if used >= max_len:
            break
        b = min(b, a + (max_len - used))
        out.append((a, b))
        used += b - a
    return out


def tiktok_summary_items(cfg: dict, decision: dict, candidates: list[dict]) -> list[dict]:
    """Tramos del resumen vertical cuando Claude no los eligió (decisiones anteriores a esta función):
    un tramo corto por cada mejor momento, terminando poco después del remate. Primero el más
    fuerte (gancho) y luego en el orden en que pasaron."""
    by_id = {int(c["id"]): c for c in candidates}
    guion = {int(g["candidato_id"]): g for g in decision.get("guion", []) if g["tipo"] == "clip"}
    items = []
    for m in decision.get("mejores_momentos", []):
        cand = by_id.get(int(m["candidato_id"]))
        if not cand:
            continue
        g = guion.get(int(m["candidato_id"]), {})
        a, b = float(m["inicio"]), float(m["fin"])
        mom = float(g.get("momento_clave") or 0)
        end = min(b, mom + 5) if a < mom <= b else b
        items.append({"candidato_id": int(m["candidato_id"]), "inicio": round(max(a, end - 30), 2),
                      "fin": round(end, 2), "texto_en_pantalla": m.get("titulo", ""),
                      "momento_clave": mom if a < mom <= b else 0.0,
                      "_prio": int(g.get("prioridad") or 3), "_t": cand["start_ts"] + a})
    if not items:
        return []
    hook = max(items, key=lambda it: (it["_prio"], -it["_t"]))
    rest = sorted((it for it in items if it is not hook), key=lambda it: it["_t"])
    out, used = [], 0.0
    max_s = float(cfg["edicion"].get("resumen_tiktok_max_s", 240))
    for it in [hook, *rest]:
        if used >= max_s - 3:
            break
        out.append({k: v for k, v in it.items() if not k.startswith("_")})
        used += it["fin"] - it["inicio"]
    return out


def render_tiktok_summary(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                          progress=None) -> Path | None:
    """Resumen vertical 1080x1920 de máximo edicion.resumen_tiktok_max_s: tramos cortos seguidos, sin
    tarjetas; el texto en pantalla de cada tramo cuenta la historia."""
    fecha = session["fecha"]
    if cfg["edicion"].get("tiktok_narrado", True) and any(e.get("narracion") for e in decision.get("resumen_tiktok") or []):
        # Ficha vertical: narrador en off (Piper) con contadores, flashes y citas. Si falla, el de texto.
        from . import narrator, tiktok_recap
        if narrator.available(cfg):
            try:
                out = tiktok_recap.render_recap(cfg, db, session, decision, candidates, progress)
                if out:
                    return out
            except Exception as exc:  # noqa: BLE001
                log.error("Falló el resumen TikTok narrado (%s); se arma el de texto en pantalla", exc)
        else:
            log.warning("No hay narrador (falta edge-tts: pip install -r requirements.txt): "
                        "el resumen TikTok sale con texto en pantalla")
    items = decision.get("resumen_tiktok") or tiktok_summary_items(cfg, decision, candidates)
    if not items:
        log.info("Sin tramos para el resumen de TikTok")
        return None
    folder = session_dir(cfg, fecha)
    work = folder / "render_tiktok"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    size = (1080, 1920)
    by_id = {int(c["id"]): c for c in candidates}
    guion = {int(g["candidato_id"]): g for g in decision.get("guion", []) if g["tipo"] == "clip"}
    specs: list[tuple[dict, ClipSpec]] = []
    for it in items:
        cand = by_id.get(int(it["candidato_id"]))
        spec = prepare_clip(cfg, db, session, cand, it["inicio"], it["fin"], it.get("texto_en_pantalla", "")) \
            if cand else None
        if not spec:
            continue
        g = guion.get(int(it["candidato_id"]), {})
        item = {**g, "momento_clave": it.get("momento_clave") or 0, "pantalla_dividida_con": 0}
        apply_clip_effects(cfg, db, session, spec, cand, item, by_id)
        style.direct(cfg, db, session, spec, cand, item, size)   # punch-ins a la cara y zoom a textos
        spec.whoosh = bool(specs)
        specs.append((it, spec))
    budget_sfx(cfg, [sp for _it, sp in specs])
    sfx_lib = effects.sfx_library(cfg) if cfg["edicion"].get("efectos_sonido") else None
    max_s = float(cfg["edicion"].get("resumen_tiktok_max_s", 240))
    pieces, total = [], 0.0
    try:
        for n, (it, spec) in enumerate(specs):
            remaining = max_s - total
            if remaining < 3:
                break
            if spec.kept_duration > remaining:
                spec.keep = trim_keep(spec.keep or [(0.0, spec.dur)], remaining)
            if progress:
                progress(f"resumen TikTok {n + 1}/{len(specs)}")
            text = it.get("texto_en_pantalla") or spec.titulo
            title_png = render_lower_third(text, spec.nombre, size, work / f"t_{n:03d}.png",
                                           cfg["edicion"]["fuente"]) if text else None
            out = work / f"{n:03d}.mp4"
            try:
                total += render_clip(cfg, spec, out, size, title_png, work=work, sfx_lib=sfx_lib)
            except Exception as exc:  # noqa: BLE001
                log.error("Falló el tramo %d del resumen TikTok: %s", n + 1, exc)
                continue
            pieces.append(out)
        if not pieces:
            return None
        final = folder / f"resumen_tiktok_{fecha}.mp4"
        concat_pieces(pieces, final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    db.add_output(session["id"], "resumen_tiktok", str(final), {"segundos": round(total, 1)})
    log.info("Resumen TikTok listo: %s (%.0f s)", final.name, total)
    return final


def export_tiktok_clips(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                        progress=None) -> list[Path]:
    """Un clip vertical 1080x1920 por cada 'mejor momento', con su título."""
    fecha = session["fecha"]
    folder = session_dir(cfg, fecha) / "clips_tiktok"
    folder.mkdir(exist_ok=True)
    size = (1080, 1920)
    by_id = {int(c["id"]): c for c in candidates}
    outs = []
    work = folder / "_render"
    work.mkdir(exist_ok=True)
    sfx_lib = effects.sfx_library(cfg) if cfg["edicion"].get("efectos_sonido") else None
    guion_by_cand: dict[int, dict] = {}
    for g in decision.get("guion", []):
        if g["tipo"] == "clip":
            guion_by_cand.setdefault(int(g["candidato_id"]), g)
    for i, m in enumerate(decision.get("mejores_momentos", []), 1):
        cand = by_id.get(int(m["candidato_id"]))
        if not cand:
            continue
        if progress:
            progress(f"clip TikTok {i}/{len(decision['mejores_momentos'])}")
        spec = prepare_clip(cfg, db, session, cand, m["inicio"], m["fin"], m["titulo"])
        if not spec:
            continue
        g = guion_by_cand.get(int(m["candidato_id"]), {})
        apply_clip_effects(cfg, db, session, spec, cand, {**g, "pantalla_dividida_con": 0}, by_id)
        style.direct(cfg, db, session, spec, cand, g, size)
        out = folder / f"{i:02d}_{slugify(m['titulo'] or cand['nombre'])}.mp4"
        title_png = render_lower_third(m["titulo"], spec.nombre, size, folder / f"_t{i:02d}.png",
                                       cfg["edicion"]["fuente"])
        try:
            render_clip(cfg, spec, out, size, title_png, work=work, sfx_lib=sfx_lib)
            outs.append(out)
            db.add_output(session["id"], "clip_tiktok", str(out), {"titulo": m["titulo"]})
        except Exception as exc:  # noqa: BLE001
            log.error("Falló el clip TikTok %d: %s", i, exc)
        finally:
            title_png.unlink(missing_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    return outs
