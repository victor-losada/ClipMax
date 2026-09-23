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

from . import tools
from .cards import render_card, render_lower_third
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
                          pad: float = 0.35, tail: float = 1.5, min_piece: float = 0.6) -> list[tuple[float, float]]:
    """Qué conservar de un clip dado dónde hay voz: se eliminan huecos sin voz > max_gap.

    - Cada tramo de voz se amplía `pad` s a cada lado (whisper no es exacto al milisegundo).
    - Tras la última frase se deja `tail` s para la reacción (risa, grito).
    - Huecos <= max_gap se conservan: son pausas naturales del habla.
    """
    if not speech:
        return [(0.0, clip_len)]
    padded = [(max(0.0, a - pad), min(clip_len, b + pad)) for a, b in speech if b > a]
    if not padded:
        return [(0.0, clip_len)]
    merged = merge_intervals(padded, max_gap)
    last_a, last_b = merged[-1]
    merged[-1] = (last_a, min(clip_len, last_b + tail))
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a >= min_piece] or [(0.0, clip_len)]


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


def build_clip_filtergraph(keep: list[tuple[float, float]], layout: str, fps: int,
                           title_dur: float | None) -> str:
    """filter_complex completo: cortes internos + concat + encuadre + título + audio normalizado."""
    k = len(keep)
    parts = [f"[0:v]setpts=PTS-STARTPTS,split={k}" + "".join(f"[vs{i}]" for i in range(k)),
             f"[0:a]asetpts=PTS-STARTPTS,asplit={k}" + "".join(f"[as{i}]" for i in range(k))]
    for i, (a, b) in enumerate(keep):
        ln = b - a
        fade = min(0.04, ln / 4)
        parts.append(f"[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(
            f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0.0, ln - fade):.3f}:d={fade:.3f}[a{i}]"
        )
    parts.append("".join(f"[v{i}][a{i}]" for i in range(k)) + f"concat=n={k}:v=1:a=1[vc][ac]")
    parts.append(layout)  # [vc] -> ... -> [vl]
    if title_dur:
        t = title_dur
        parts.append(f"[1:v]format=rgba,fade=t=in:st=0.2:d=0.3:alpha=1,"
                     f"fade=t=out:st={max(0.5, t - 0.5):.2f}:d=0.4:alpha=1[ttl]")
        parts.append("[vl][ttl]overlay=0:0:eof_action=pass[vt]")
        parts.append(f"[vt]fps={fps},format=yuv420p[vout]")
    else:
        parts.append(f"[vl]fps={fps},format=yuv420p[vout]")
    parts.append("[ac]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,"
                 "aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")
    return ";".join(parts)


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
                 titulo: str = "") -> ClipSpec | None:
    """Ubica el tramo en disco y calcula qué partes conservar (sin silencios largos)."""
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
    method = ed["metodo_silencios"]
    max_gap = float(ed["silencio_max_s"])
    if method == "transcripcion":
        segs = db.segments(session["id"], cand["slug"], wall0, wall0 + dur)
        speech = [(max(0.0, s["start_ts"] - wall0), min(dur, s["end_ts"] - wall0)) for s in segs]
        spec.keep = speech_keep_intervals(dur, speech, max_gap)
    elif method == "audio":
        spec.keep = audio_keep_intervals(path, start, dur, float(ed["umbral_silencio_db"]), max_gap)
    else:
        spec.keep = [(0.0, dur)]
    return spec


def render_clip(cfg: dict, spec: ClipSpec, out: Path, out_size: tuple[int, int],
                title_png: Path | None = None) -> float:
    fps = int(cfg["edicion"]["fps"])
    streamer = get_streamer(cfg, spec.slug) or {"modo": "juego_cara", "camara": None}
    modo = streamer.get("modo", "juego_cara")
    if modo == "cara" and not streamer.get("camara"):
        log.warning("[%s] modo 'cara' sin recuadro de cámara configurado; uso el stream completo", spec.slug)
    layout = layout_filter("vc", "vl", _src_size(spec.path), out_size, modo, streamer.get("camara"))
    kept = spec.kept_duration
    title_dur = min(5.0, kept) if title_png else None
    graph = build_clip_filtergraph(spec.keep, layout, fps, title_dur)
    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{spec.file_start:.3f}", "-t", f"{spec.dur:.3f}", "-i", spec.path]
    if title_png:
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{title_dur:.2f}", "-i", str(title_png)]
    cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]", *encode_args(cfg), str(out)]
    tools.run(cmd, timeout=max(600, spec.dur * 20))
    return kept


def _reading_time(text: str) -> float:
    return min(10.0, max(3.0, len(text.split()) / 2.6 + 1.2))


def render_card_piece(cfg: dict, text: str, out: Path, out_size: tuple[int, int], *,
                      bg: Path | None = None, label: str = "", big: bool = False,
                      voice: bool = True) -> float:
    w, h = out_size
    fps = int(cfg["edicion"]["fps"])
    png = out.with_suffix(".png")
    render_card(text, out_size, png, bg_image=bg, label=label, font_path=cfg["edicion"]["fuente"], big=big)
    wav = out.with_suffix(".wav")
    has_voice = voice and synthesize(cfg, text, wav)
    dur = _reading_time(text)
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

def render_summary(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                   progress=None) -> Path:
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
                    specs[idx] = spec

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
                total += render_clip(cfg, spec, out, size, title_png)
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


def export_tiktok_clips(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                        progress=None) -> list[Path]:
    """Un clip vertical 1080x1920 por cada 'mejor momento', con su título."""
    fecha = session["fecha"]
    folder = session_dir(cfg, fecha) / "clips_tiktok"
    folder.mkdir(exist_ok=True)
    size = (1080, 1920)
    by_id = {int(c["id"]): c for c in candidates}
    outs = []
    for i, m in enumerate(decision.get("mejores_momentos", []), 1):
        cand = by_id.get(int(m["candidato_id"]))
        if not cand:
            continue
        if progress:
            progress(f"clip TikTok {i}/{len(decision['mejores_momentos'])}")
        spec = prepare_clip(cfg, db, session, cand, m["inicio"], m["fin"], m["titulo"])
        if not spec:
            continue
        out = folder / f"{i:02d}_{slugify(m['titulo'] or cand['nombre'])}.mp4"
        title_png = render_lower_third(m["titulo"], spec.nombre, size, folder / f"_t{i:02d}.png",
                                       cfg["edicion"]["fuente"])
        try:
            render_clip(cfg, spec, out, size, title_png)
            outs.append(out)
            db.add_output(session["id"], "clip_tiktok", str(out), {"titulo": m["titulo"]})
        except Exception as exc:  # noqa: BLE001
            log.error("Falló el clip TikTok %d: %s", i, exc)
        finally:
            title_png.unlink(missing_ok=True)
    return outs
