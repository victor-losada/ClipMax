"""Diagnóstico de sincronía audio/video con las grabaciones reales del usuario.

Responde en la máquina del usuario (su ffmpeg, sus archivos) a: ¿el video que arma ClipMax queda
alineado con la grabación? Para eso renderiza un clip de prueba con el mismo código de edición y
lo compara con la fuente:

- Referencia: la fuente leída con marcas de tiempo absolutas (-copyts + trim), que no depende de
  cómo ffmpeg salta al punto de corte.
- Video: para varios instantes del clip busca el fotograma de la fuente más parecido.
- Audio: correlación de la envolvente de volumen.

Si video y audio se corren lo mismo, el render está en sincronía (aunque el corte caiga unas
centésimas antes o después). Si no, la diferencia es el desfase que agrega la edición.
Sin numpy: imágenes de 32x18 y envolventes cada 10 ms bastan.
"""

from __future__ import annotations

import array
import json
import math
import tempfile
from pathlib import Path

from . import editor, tools
from .db import Database

W, H, FPS = 48, 27, 30
HOP = 0.01
MIN_CONF = 0.5   # correlación mínima para confiar en una medición


def _frames(path: str, pre: list[str], vf: str) -> list[bytes]:
    proc = tools.run([tools.ffmpeg(), "-v", "error", *pre, "-i", path, "-an",
                      "-vf", f"{vf}fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], timeout=300)
    raw = proc.stdout or b""
    n = W * H
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


def _envelope(path: str, pre: list[str], af: str) -> list[float]:
    proc = tools.run([tools.ffmpeg(), "-v", "error", *pre, "-i", path, "-vn",
                      "-af", f"{af}aresample=8000", "-ac", "1", "-f", "s16le", "-"], timeout=300)
    pcm = array.array("h")
    pcm.frombytes((proc.stdout or b"")[: len(proc.stdout or b"") // 2 * 2])
    hop = int(8000 * HOP)
    env = []
    for i in range(0, len(pcm) - hop + 1, hop):
        chunk = pcm[i:i + hop]
        env.append(math.log(math.sqrt(sum(x * x for x in chunk) / hop) + 1.0))
    mean = sum(env) / len(env) if env else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in env) / len(env)) if env else 1.0
    return [(x - mean) / (sd or 1.0) for x in env]


def _motion(frames: list[bytes]) -> list[float]:
    """Cuánto cambia la imagen de un fotograma al siguiente (normalizado). Sirve para alinear
    aunque haya escenas quietas, donde comparar fotogramas sueltos es ambiguo."""
    m = [sum(abs(a - b) for a, b in zip(frames[i], frames[i - 1])) / len(frames[i]) for i in range(1, len(frames))]
    if not m:
        return []
    mean = sum(m) / len(m)
    sd = math.sqrt(sum((x - mean) ** 2 for x in m) / len(m)) or 1.0
    return [(x - mean) / sd for x in m]


def _best_lag(ref: list[float], seg: list[float]) -> tuple[int, float]:
    """Desplazamiento de `seg` dentro de `ref` con mayor correlación de Pearson (-1..1)."""
    n = len(seg)
    ms = sum(seg) / n
    ds = [x - ms for x in seg]
    ss = math.sqrt(sum(x * x for x in ds)) or 1.0
    best, lag = -2.0, 0
    for k in range(len(ref) - n + 1):
        w = ref[k:k + n]
        mw = sum(w) / n
        sw = math.sqrt(sum((x - mw) ** 2 for x in w)) or 1.0
        c = sum((w[i] - mw) * ds[i] for i in range(n)) / (sw * ss)
        if c > best:
            best, lag = c, k
    return lag, best


def measure_render(cfg: dict, source: str, start: float, dur: float = 12.0, margin: float = 3.0) -> dict:
    """Renderiza [start, start+dur] de `source` como lo hace la edición y mide cuánto se corre cada pista."""
    with tempfile.TemporaryDirectory(prefix="clipmax_diag_") as tmp:
        out = Path(tmp) / "prueba.mp4"
        spec = editor.ClipSpec("diag", "diag", source, start, dur, 0.0, keep=[(0.0, dur)])
        editor._render_clip(cfg, spec, out, (640, 360), None, Path(tmp), None, False)
        a, b = max(0.0, start - margin), start + dur + margin
        t0 = _start_time(source)   # con -copyts las marcas son absolutas (un .ts empieza en ~1.4 s)
        pre = ["-ss", f"{max(0.0, a - 15):.3f}", "-copyts"]
        ref_v = _frames(source, pre, f"trim=start={a + t0:.3f}:end={b + t0:.3f},")
        ref_a = _envelope(source, pre, f"atrim=start={a + t0:.3f}:end={b + t0:.3f},")
        out_v = _frames(str(out), [], "")
        out_a = _envelope(str(out), [], "")
    # Video: se alinea la curva de movimiento del clip (sin el primer y último segundo) con la de la
    # fuente. motion[i] es el cambio entre los fotogramas i e i+1 -> el índice i vale i/FPS.
    ref_m, out_m = _motion(ref_v), _motion(out_v)
    seg_m = out_m[FPS: max(FPS, len(out_m) - FPS)]
    video = vcorr = None
    if seg_m and len(ref_m) > len(seg_m):
        lag, vcorr = _best_lag(ref_m, seg_m)
        video = round(a + lag / FPS - (start + 1.0), 3)
    seg = out_a[int(1.0 / HOP): int((dur - 1.0) / HOP)]
    lag, corr = _best_lag(ref_a, seg) if seg and len(ref_a) > len(seg) else (0, 0.0)
    audio = round(a + lag * HOP - (start + 1.0), 3)
    return {"video_s": video, "audio_s": audio, "confianza_video": round(vcorr or 0.0, 2),
            "confianza_audio": round(corr, 2),
            "desfase_s": round(audio - video, 3) if video is not None else None}


def _start_time(path: str) -> float:
    probe = tools.ffprobe()
    if not probe:
        return 0.0
    proc = tools.run([probe, "-v", "error", "-show_entries", "format=start_time", "-of", "json", path],
                     timeout=60, check=False)
    try:
        return float(json.loads(proc.stdout or b"{}")["format"]["start_time"])
    except (KeyError, ValueError, TypeError):
        return 0.0


def stream_info(path: str) -> dict:
    probe = tools.ffprobe()
    if not probe:
        return {}
    proc = tools.run([probe, "-v", "error", "-show_entries",
                      "stream=codec_type,avg_frame_rate,start_time,duration", "-of", "json", path],
                     timeout=120, check=False)
    try:
        streams = json.loads(proc.stdout or b"{}").get("streams", [])
    except ValueError:
        return {}
    out = {}
    for s in streams:
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in out:
            out[kind] = {k: s.get(k) for k in ("avg_frame_rate", "start_time", "duration")}
    return out


def _fps(rate) -> str:
    try:
        n, d = str(rate).split("/")
        return f"{float(n) / float(d):.2f}"
    except (ValueError, ZeroDivisionError):
        return "?"


def _num(x) -> str:
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "?"


def _version_line(cmd: list[str]) -> str:
    try:
        proc = tools.run(cmd, timeout=60, check=False)
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        return text.strip().splitlines()[0] if text.strip() else "?"
    except Exception as exc:  # noqa: BLE001
        return f"no disponible ({exc})"


def run(cfg: dict, db: Database, fecha: str) -> str:
    """Informe en texto (para la web o la consola)."""
    lines = [f"Diagnóstico de sincronía · {fecha}", ""]
    lines.append("ffmpeg: " + _version_line([tools.ffmpeg(), "-hide_banner", "-version"]))
    try:
        wcli = tools.whisper_cli(cfg)
        helptext = tools.run([wcli, "--help"], timeout=60, check=False)
        text = ((helptext.stdout or b"") + (helptext.stderr or b"")).decode("utf-8", "replace")
        lines.append("whisper-cli: " + ("con alineación DTW (subtítulos precisos)" if "-dtw" in text
                                        else "SIN DTW: actualiza whisper.cpp para subtítulos precisos"))
    except tools.ToolMissing as exc:
        lines.append(f"whisper-cli: no encontrado ({exc})")
    ed = cfg["edicion"]
    lines.append(f"Ajustes: desfase_audio_s={ed.get('desfase_audio_s', 0)} · "
                 f"subtitulos_desfase_s={ed.get('subtitulos_desfase_s', 0)}")
    sess = db.get_session_by_date(fecha)
    if not sess:
        return "\n".join(lines + ["", "No hay sesión para esa fecha."])
    finals = sorted({p["final_path"] for p in db.list_parts(sess["id"]) if p.get("final_path")})
    finals = [f for f in finals if Path(f).exists()]
    if not finals:
        return "\n".join(lines + ["", "Esta sesión no tiene grabaciones finales (MP4) todavía."])
    lines += ["", "Grabaciones:"]
    for f in finals:
        info = stream_info(f)
        v, a = info.get("video", {}), info.get("audio", {})
        try:
            gap = float(a.get("duration") or 0) - float(v.get("duration") or 0)
            extra = f" · audio {'más largo' if gap > 0 else 'más corto'} que el video por {abs(gap):.1f}s" \
                if abs(gap) > 1.0 else " · audio y video con la misma duración"
        except (TypeError, ValueError):
            extra = ""
        lines.append(f"- {Path(f).name}: {_fps(v.get('avg_frame_rate'))} fps, video {_num(v.get('duration'))}s, "
                     f"audio {_num(a.get('duration'))}s{extra}")
    lines += ["", "Prueba de edición (se arma un clip y se compara con la grabación):"]
    verdicts: list[bool] = []
    measured: list[float] = []
    for f in finals[:3]:
        try:
            total = float(stream_info(f).get("video", {}).get("duration") or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total < 60:
            continue
        for frac in (0.3, 0.7):
            start = round(total * frac, 2) + 0.37   # a propósito, lejos de un keyframe
            try:
                r = measure_render(cfg, f, start)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"- {Path(f).name} @ {start:.0f}s: no se pudo medir ({str(exc)[-200:]})")
                continue
            if r["desfase_s"] is None or min(r["confianza_video"], r["confianza_audio"]) < MIN_CONF:
                lines.append(f"- {Path(f).name} @ {start:.0f}s: tramo sin suficiente movimiento o sonido "
                             "para medir")
                continue
            ok = abs(r["desfase_s"]) <= 0.1
            verdicts.append(ok)
            measured.append(r["desfase_s"])
            lines.append(f"- {Path(f).name} @ {start:.0f}s: video {r['video_s']:+.2f}s · audio "
                         f"{r['audio_s']:+.2f}s → desfase audio-video {r['desfase_s']:+.2f}s "
                         f"{'OK' if ok else '⚠'} (confianza {r['confianza_video']}/{r['confianza_audio']})")
    lines.append("")
    if not verdicts:
        lines.append("No hubo tramos suficientes para medir.")
    elif all(verdicts):
        lines.append("Resultado: la edición conserva la sincronía de la grabación. Si aun así ves desfase, "
                     "abre el MP4 de la grabación completa: si ahí también se ve corrido, viene del directo; "
                     "compénsalo con edicion.desfase_audio_s.")
    else:
        bad = sorted(measured)
        med = bad[len(bad) // 2]
        when = "tarde" if med < 0 else "antes"
        sugerido = round(float(ed.get("desfase_audio_s") or 0.0) + med, 2)
        lines.append(f"Resultado: en esta máquina la edición deja la voz {abs(med):.2f}s {when} que la imagen. "
                     f"Mientras se corrige, en Configuración → Edición pon «Desfase del audio» = {sugerido} "
                     "y manda este informe.")
    return "\n".join(lines)
