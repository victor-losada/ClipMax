"""Resumen diario para TikTok narrado en off (ficha "resumen vertical, formato corto").

Composición: 1080x1920, el clip 16:9 centrado (~32 % de la altura) sobre el mismo clip
desenfocado; contadores en las esquinas superiores del clip; subtítulos justo debajo.

Estructura: intro (revelado pixel, logo opcional, foto del día con paneo-zoom lento, contadores
grandes con las cifras del día y flashes en cada cifra) -> un evento por hecho (conector + hecho
narrado sobre cortes rápidos, ráfaga de micro-cortes y cuadro de impacto en muertes/explosiones,
flash de color en la palabra clave, el contador sube en esa palabra, y a veces una cita con la voz
real del streamer, con punch-ins a su cara) -> cierre (flash, primerísimo plano de 0.25 s,
llamada a seguir con flecha animada y fade de 3 s).

Audio: narración (Piper) al frente, el juego muy bajo, sin música; mezcla comprimida.
"""

from __future__ import annotations

import logging
import math
import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from . import effects, narrator, style, tools
from .cards import _font, _strip_unrenderable
from .config import PROJECT_ROOT, get_streamer, session_dir, streamer_name
from .db import Database
from .editor import SYNC_AUDIO, ClipSpec, _src_size, audio_shift, concat_pieces, encode_args
from .mentions import normalize
from .recorder import locate_range, snapshot_from_file

log = logging.getLogger(__name__)

W, H = 1080, 1920
CLIP_H = 608
CLIP_Y = (H - CLIP_H) // 2
SUB_Y = CLIP_Y + CLIP_H + 34
YELLOW = "#FFD400"
FLASH = {"muerte": "0xFF2A2A", "explosion": "0xFF2A2A", "alianza": "0x39FF5A", "logro": "0x39FF5A",
         "anuncio": "0xFFD400", "pique": "0xFFD400", "traicion": "0xFF8A1F", "otro": "0xFFD400"}
PATTERN = [1.6, 2.2, 1.2, 2.6, 0.9, 1.8, 1.4, 2.0, 0.8]      # duración de planos: media ~1.6 s
NUMEROS = set("uno una dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince "
              "veinte treinta cuarenta cincuenta cien mil primer primero segundo tercero".split())
LOGO = PROJECT_ROOT / "assets" / "logo.png"
PERFIL = PROJECT_ROOT / "assets" / "perfil.png"


# ---------------------------------------------------------------------------
# Gráficos
# ---------------------------------------------------------------------------
def _icon(draw: ImageDraw.ImageDraw, kind: str, x: int, y: int, s: int) -> None:
    """Íconos dibujados (sin depender de fuentes de emojis)."""
    k = s / 100
    P = lambda pts: [(x + px * k, y + py * k) for px, py in pts]  # noqa: E731
    if kind == "calavera":
        draw.ellipse(P([(12, 5), (88, 75)]), fill="white", outline="black", width=int(4 * k) + 1)
        draw.rectangle(P([(28, 62), (72, 92)]), fill="white", outline="black", width=int(4 * k) + 1)
        draw.ellipse(P([(24, 30), (46, 52)]), fill="black")
        draw.ellipse(P([(54, 30), (76, 52)]), fill="black")
        for tx in (38, 50, 62):
            draw.line(P([(tx, 72), (tx, 92)]), fill="black", width=int(3 * k) + 1)
    elif kind == "corazon":
        draw.ellipse(P([(8, 12), (52, 56)]), fill="#FF3355")
        draw.ellipse(P([(48, 12), (92, 56)]), fill="#FF3355")
        draw.polygon(P([(10, 42), (90, 42), (50, 92)]), fill="#FF3355")
    elif kind == "espada":
        draw.polygon(P([(82, 8), (92, 18), (40, 70), (30, 60)]), fill="#DDE6F0", outline="black")
        draw.line(P([(22, 52), (48, 78)]), fill="#8B5A2B", width=int(10 * k) + 1)
        draw.line(P([(30, 70), (12, 88)]), fill="#5A3A1A", width=int(10 * k) + 1)
    elif kind == "diamante":
        draw.polygon(P([(50, 8), (90, 40), (50, 94), (10, 40)]), fill="#3FE0FF", outline="black", width=int(4 * k) + 1)
        draw.polygon(P([(50, 8), (66, 40), (50, 94), (34, 40)]), fill="#9FF3FF")
    elif kind == "casa":
        draw.polygon(P([(50, 8), (92, 46), (8, 46)]), fill="#FF8A1F", outline="black")
        draw.rectangle(P([(18, 46), (82, 92)]), fill="#E8D8B0", outline="black", width=int(4 * k) + 1)
        draw.rectangle(P([(42, 62), (58, 92)]), fill="#8B5A2B")
    elif kind == "rayo":
        draw.polygon(P([(58, 4), (18, 56), (46, 56), (36, 96), (82, 38), (54, 38)]), fill=YELLOW, outline="black")
    elif kind == "trofeo":
        draw.polygon(P([(22, 10), (78, 10), (70, 52), (30, 52)]), fill="#FFC21A", outline="black")
        draw.rectangle(P([(44, 52), (56, 74)]), fill="#FFC21A")
        draw.rectangle(P([(28, 74), (72, 90)]), fill="#C88A00", outline="black")
    else:  # estrella
        pts = []
        for i in range(10):
            r = 46 if i % 2 == 0 else 20
            a = -math.pi / 2 + i * math.pi / 5
            pts.append((50 + r * math.cos(a), 52 + r * math.sin(a)))
        draw.polygon(P(pts), fill=YELLOW, outline="black")


def _counter_box(img: Image.Image, x: int, y: int, label: str, icon: str, value: int, scale: float,
                 highlight: bool) -> tuple[int, int]:
    draw = ImageDraw.Draw(img)
    font_num = _font(str(style.CAPTION_FONT), int(64 * scale))
    font_lbl = _font(str(style.CAPTION_FONT), int(24 * scale))
    num = str(value)
    ic = int(62 * scale)
    pad = int(12 * scale)
    tw = max(draw.textlength(num, font=font_num) + ic + pad * 3, draw.textlength(label, font=font_lbl) + pad * 2)
    bh = int(ic + font_lbl.size + pad * 2.2)
    draw.rounded_rectangle([x, y, x + tw, y + bh], radius=int(16 * scale), fill=(0, 0, 0, 170))
    _icon(draw, icon, x + pad, y + pad, ic)
    draw.text((x + pad * 2 + ic, y + pad - int(6 * scale)), num, font=font_num,
              fill=(255, 212, 0, 255) if highlight else (255, 255, 255, 255),
              stroke_width=max(2, int(4 * scale)), stroke_fill=(0, 0, 0, 255))
    draw.text((x + pad, y + pad + ic + int(2 * scale)), label, font=font_lbl, fill=(255, 212, 0, 255),
              stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return int(tw), bh


def counters_png(counters: list[dict], values: dict[str, int], out: Path, *, big: bool = False,
                 highlight: str = "") -> Path:
    """Contadores en las esquinas superiores del clip (o grandes y centrados en la intro)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if big:
        scale = 1.7
        x, y = 60, CLIP_Y + 80
        for c in counters:
            w, h = _counter_box(img, x, y, c["etiqueta"], c["icono"], values.get(c["id"], 0), scale, True)
            x += w + 30
            if x > W - 300:
                x, y = 60, y + h + 24
    else:
        slots = [(18, CLIP_Y + 16, "L"), (W - 18, CLIP_Y + 16, "R"), (18, CLIP_Y + 150, "L"), (W - 18, CLIP_Y + 150, "R")]
        for c, (sx, sy, side) in zip(counters, slots):
            scale = 1.25 if c["id"] == highlight else 1.0
            tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            w, h = _counter_box(tmp, 0, 0, c["etiqueta"], c["icono"], values.get(c["id"], 0), scale, c["id"] == highlight)
            box = tmp.crop((0, 0, w, h))
            img.alpha_composite(box, (sx if side == "L" else sx - w, sy))
    img.save(out)
    return out


def impact_png(out: Path, seed: int = 7) -> Path:
    """Cuadro de impacto: líneas radiales blancas tipo manga sobre el clip (centro libre)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    cx, cy = W / 2, CLIP_Y + CLIP_H / 2
    for _ in range(46):
        a = rnd.uniform(0, 2 * math.pi)
        width = rnd.uniform(0.012, 0.035)
        r0 = rnd.uniform(170, 260)
        r1 = 900
        pts = [(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
               (cx + r1 * math.cos(a - width), cy + r1 * math.sin(a - width)),
               (cx + r1 * math.cos(a + width), cy + r1 * math.sin(a + width))]
        draw.polygon(pts, fill=(255, 255, 255, 235))
    img.crop((0, 0, W, H)).save(out)
    return out


def text_png(text: str, out: Path, size: int = 150, color: tuple = (255, 212, 0, 255), y: int | None = None) -> Path:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = _strip_unrenderable(text).upper()
    font = _font(str(style.CAPTION_FONT), size)
    while draw.textlength(text, font=font) > W * 0.9 and size > 40:
        size = int(size * 0.9)
        font = _font(str(style.CAPTION_FONT), size)
    tw = draw.textlength(text, font=font)
    draw.text(((W - tw) / 2, y if y is not None else CLIP_Y - size - 40), text, font=font, fill=color,
              stroke_width=max(4, size // 14), stroke_fill=(0, 0, 0, 255))
    img.save(out)
    return out


def perfil_png(src: Path, out: Path) -> Path:
    """La tarjeta de perfil (assets/perfil.png) abajo al centro, debajo de los subtítulos."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    card = Image.open(src).convert("RGBA")
    card.thumbnail((int(W * 0.8), 220))
    img.alpha_composite(card, ((W - card.width) // 2, SUB_Y + 230))
    img.save(out)
    return out


def arrow_png(out: Path) -> Path:
    img = Image.new("RGBA", (260, 150), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pts = [(10, 50), (160, 50), (160, 10), (250, 75), (160, 140), (160, 100), (10, 100)]
    draw.polygon(pts, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=8)
    img.save(out)
    return out


# ---------------------------------------------------------------------------
# Subtítulos
# ---------------------------------------------------------------------------
def highlight_set(cfg: dict) -> set[str]:
    names = set()
    for s in cfg["streamers"]:
        for n in [s["nombre"], s["slug"], *s["alias"]]:
            names |= set(normalize(n).split())
    return names | NUMEROS


def build_recap_ass(words: list[tuple[float, float, str]], keep_yellow: set[str], family: str) -> str:
    """Bloques de 1-3 palabras bajo el clip; la palabra dicha en amarillo; nombres y cifras siempre en amarillo."""
    fs = int(H * 0.05)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Sub,{family},{fs},&H00FFFFFF,&H000000FF,&H00101010,&H96000000,0,0,0,0,"
        f"100,100,1,0,1,{max(4, fs // 12)},{max(2, fs // 28)},8,{int(W * 0.05)},{int(W * 0.05)},{SUB_Y},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    yellow = style.ass_color(YELLOW)
    lines = []
    chunks = effects.chunk_words(words, max_words=3, max_chars=16, max_gap=0.35)
    for ci, chunk in enumerate(chunks):
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else chunk[-1][1] + 0.3
        texts = [effects._ass_text(w[2]) for w in chunk]
        fixed = [bool(re.sub(r"\W", "", normalize(w[2])) in keep_yellow or re.search(r"\d", w[2])) for w in chunk]
        for i, (a, _b, _w) in enumerate(chunk):
            end = chunk[i + 1][0] if i + 1 < len(chunk) else min(nxt, chunk[-1][1] + 0.3)
            if end - a < 0.04:
                continue
            parts = []
            for j, t in enumerate(texts):
                if j == i:
                    parts.append(f"{{\\c{yellow}\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}}{t}{{\\r}}")
                elif fixed[j]:
                    parts.append(f"{{\\c{yellow}}}{t}{{\\r}}")
                else:
                    parts.append(t)
            lines.append(f"Dialogue: 0,{effects._ass_time(a)},{effects._ass_time(end)},Sub,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


def _write_ass(cfg: dict, words: list, work: Path, name: str) -> str | None:
    from PIL import ImageFont

    if not words or not style.CAPTION_FONT.exists():
        return None
    fonts = work / "fonts"
    fonts.mkdir(exist_ok=True)
    if not (fonts / style.CAPTION_FONT.name).exists():
        shutil.copy2(style.CAPTION_FONT, fonts / style.CAPTION_FONT.name)
    family = ImageFont.truetype(str(style.CAPTION_FONT), 20).getname()[0]
    (work / f"{name}.ass").write_text(build_recap_ass(words, highlight_set(cfg), family), encoding="utf-8")
    return f"{name}.ass"


# ---------------------------------------------------------------------------
# Planos
# ---------------------------------------------------------------------------
@dataclass
class Shot:
    a: float                  # segundos del candidato
    b: float
    vol: float = 0.10         # audio del clip (bajo la narración)
    impact: bool = False


def _lengths(dur: float, offset: int = 0) -> list[float]:
    """Duraciones de plano que suman `dur` siguiendo el patrón irregular (sin planos < 0.35 s)."""
    lens, acc, i = [], 0.0, offset
    while acc < dur - 0.05:
        d = min(PATTERN[i % len(PATTERN)], dur - acc)
        if d < 0.35 and lens:
            lens[-1] += d
            break
        lens.append(d)
        acc += d
        i += 1
    return lens


def plan_shots(total: float, a: float, b: float, key: float | None, burst_at: float | None) -> list[Shot]:
    """Cortes rápidos (media ~1.6 s) que cubren `total` segundos con material de [a, b], en orden y
    cerca del momento clave. Con `burst_at` (segundo de la narración donde se dice la palabra
    clave de una muerte/explosión) hay una ráfaga de micro-cortes con el cuadro de impacto justo ahí."""
    key = key if key is not None and a <= key <= b else max(a, b - 3.0)
    end = min(b, key + 1.5)
    window = max(8.0, total * 3.0)        # el material sale de los segundos previos al momento clave

    def spread(lens: list[float], lo: float, hi: float, align_end: bool) -> list[Shot]:
        lo, hi = max(a, lo), min(b, hi)
        need = sum(lens)
        shots: list[Shot] = []
        if hi - lo >= need:
            if len(lens) > 1:
                gap = (hi - lo - need) / (len(lens) - 1)
                t = lo
            else:
                gap, t = 0.0, (hi - need if align_end else lo)
            for d in lens:
                shots.append(Shot(t, t + d))
                t += d + gap
            return shots
        t = max(a, hi - need)                 # poco material: planos seguidos (y se repite si hace falta)
        for d in lens:
            if t + d > b:
                t = a
            shots.append(Shot(t, min(b, t + d)))
            t += d
        return shots

    if burst_at is None or total < 3.0:
        return spread(_lengths(total), end - window, end, True)
    burst = [Shot(key - 0.6, key - 0.3), Shot(key - 0.3, key), Shot(key, key + 0.1, impact=True),
             Shot(key - 0.3, key), Shot(key, key + 0.35)]
    burst = [Shot(max(a, s.a), min(b, s.b), s.vol, s.impact) for s in burst if min(b, s.b) - max(a, s.a) > 0.05]
    blen = sum(s.b - s.a for s in burst)
    lead = sum(s.b - s.a for s in burst[:next((i for i, s in enumerate(burst) if s.impact), 0)])
    before = max(0.0, min(burst_at - lead, total - blen))     # el impacto cae en la palabra clave
    after = max(0.0, total - before - blen)
    pre = spread(_lengths(before), key - 0.6 - window, key - 0.6, True) if before > 0.05 else []
    post = spread(_lengths(after, 3), key + 0.35, key + 0.35 + after * 2, False) if after > 0.05 else []
    return pre + burst + post


# ---------------------------------------------------------------------------
# Render de piezas
# ---------------------------------------------------------------------------
@dataclass
class Piece:
    path: Path
    dur: float


@dataclass
class Ctx:
    cfg: dict
    db: Database
    session: dict
    work: Path
    counters: list[dict]
    fps: int = 30
    perfil: Path | None = None
    pieces: list[Piece] = field(default_factory=list)


def _audio_master(label_in: str, label_out: str, fade_out_at: float | None = None) -> str:
    fade = f",afade=t=out:st={fade_out_at:.2f}:d=3" if fade_out_at is not None else ""
    return (f"[{label_in}]acompressor=threshold=-24dB:ratio=4:attack=5:release=120:makeup=4,"
            f"loudnorm=I=-12:TP=-1:LRA=5,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo{fade}[{label_out}]")


def _composite(src: str, dst: str, fps: int, tag: str) -> str:
    """Clip 16:9 centrado (ancho completo) sobre el mismo clip desenfocado y estirado."""
    return (f"[{src}]split=2[{tag}f][{tag}b];"
            f"[{tag}b]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            f"scale={W}:{H},eq=brightness=-0.10,setsar=1[{tag}bg];"
            f"[{tag}f]scale={W}:{CLIP_H}:flags=bicubic,setsar=1[{tag}fg];"
            f"[{tag}bg][{tag}fg]overlay=0:{CLIP_Y},fps={fps}[{dst}]")


def _keyword_time(nar: narrator.Narration, keyword: str) -> float:
    """Momento de la palabra clave en la narración (primera palabra si son varias)."""
    target = normalize(keyword).split()
    if target:
        for a, _b, w in nar.words:
            if normalize(w) == target[0]:
                return a
    return nar.words[len(nar.words) * 2 // 3][0] if nar.words else nar.dur * 0.6


def _locate_shots(ctx: Ctx, cand: dict, shots: list[Shot]) -> list[tuple[Shot, str, float, float]]:
    """(plano, archivo, segundo del archivo, duración) de cada plano que tiene video."""
    off = float(ctx.cfg["grabacion"]["desfase_chat_s"])
    out = []
    for s in shots:
        t0, t1 = cand["start_ts"] + s.a, cand["start_ts"] + s.b
        loc = locate_range(ctx.db, ctx.session["id"], cand["slug"], t0 - 0.6, t1 + 0.6, off)
        if not loc:
            continue
        path, fstart, fdur, wall0 = loc
        a = max(0.0, t0 - wall0)
        b = min(fdur, t1 - wall0)
        if b - a >= 0.05:
            out.append((s, path, fstart + a, b - a))
    return out


def render_event(ctx: Ctx, idx: int, ev: dict, cand: dict, values_before: dict[str, int]) -> dict[str, int]:
    """Un hecho: narración sobre cortes rápidos (+ cita con la voz real) con contador, flash e impacto."""
    cfg, db, session, work = ctx.cfg, ctx.db, ctx.session, ctx.work
    fps = ctx.fps
    nar = narrator.narrate(cfg, ev["narracion"], work / f"ev{idx:02d}.wav")
    D = nar.dur + 0.05
    kw_t = _keyword_time(nar, ev.get("palabra_clave") or "")
    violent = ev.get("tipo_evento") in ("muerte", "explosion")
    plan = plan_shots(D, ev["inicio"], ev["fin"], ev.get("momento_clave") or None, kw_t if violent else None)
    narrated = _locate_shots(ctx, cand, plan)
    if not narrated:
        raise RuntimeError(f"sin video para el hecho {idx + 1}")
    quote = []
    if ev.get("cita_fin", 0) > ev.get("cita_inicio", 0):
        quote = _locate_shots(ctx, cand, [Shot(ev["cita_inicio"], ev["cita_fin"], vol=1.0)])
    shots = narrated + quote
    base = _src_size(narrated[0][1])

    # Cada plano es una entrada con su propio -ss: se lee solo lo que se usa y sin búferes enormes.
    # Sincronía como en editor._render_clip: fps/aresample alinean audio y video al cero del corte.
    extra = max(0.0, -float(cfg["edicion"].get("desfase_audio_s") or 0.0))
    inputs: list[list[str]] = [["-i", str(nar.wav)]]
    parts, t, impact_t = [], 0.0, None
    narr_len = sum(d for _s, _p, _f, d in narrated)
    for i, (s, path, fpos, d) in enumerate(shots):
        inputs.append(["-ss", f"{fpos:.3f}", "-t", f"{d + extra:.3f}", "-i", path])
        k = len(inputs) - 1
        pad = ""
        if i == len(narrated) - 1 and narr_len < D:     # faltó video: se congela el último plano
            pad = f",tpad=stop_mode=clone:stop_duration={D - narr_len:.3f}"
        apad = f",apad=pad_dur={D - narr_len:.3f}" if pad else ""
        fade = min(0.02, d / 4)
        parts.append(f"[{k}:v]fps={fps}:start_time=0,trim=duration={d:.3f},setpts=PTS-STARTPTS,"
                     f"scale={base[0]}:{base[1]},setsar=1{pad}[sv{i}]")
        parts.append(f"[{k}:a]{SYNC_AUDIO}{audio_shift(cfg)},atrim=duration={d:.3f},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0.0, d - fade):.3f}:d={fade:.3f},"
                     f"volume={s.vol:.2f}{apad}[sa{i}]")
        if s.impact:
            impact_t = t
        t += d + (D - narr_len if pad else 0.0)
    total = t
    q0 = total - sum(d for _s, _p, _f, d in quote)
    n = len(shots)
    parts.append("".join(f"[sv{i}][sa{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca]")

    # Contadores: valor de antes hasta la palabra clave, el nuevo después (con un "pop" de 0.35 s).
    after = dict(values_before)
    changed = ev.get("contador") if ev.get("contador") and ev.get("suma") else ""
    if changed:
        after[changed] = after.get(changed, 0) + int(ev["suma"])
    png_before = counters_png(ctx.counters, values_before, work / f"c{idx:02d}a.png")
    png_after = counters_png(ctx.counters, after, work / f"c{idx:02d}b.png")
    png_pop = counters_png(ctx.counters, after, work / f"c{idx:02d}p.png", highlight=changed) if changed else None

    # Subtítulos: la narración y, en la cita, lo que dice el streamer.
    words = list(nar.words)
    zooms, face, face_px = [], [], None
    if quote:
        _s, qpath, qpos, qd = quote[0]
        qwall = cand["start_ts"] + ev["cita_inicio"]
        qwords = effects.segment_words(db.segments(session["id"], cand["slug"], qwall, qwall + qd), qwall, qd)
        words += [(q0 + a, q0 + b, w) for a, b, w in qwords]
        # Ficha + facecam: en la cita, punch-ins a la cara y zoom a textos dentro del clip 16:9.
        spec = ClipSpec(cand["slug"], streamer_name(cfg, cand["slug"]), qpath, qpos, qd, qwall,
                        keep=[(0.0, qd)], words=qwords)
        qplan = style.direct(cfg, db, session, spec, None, None, (W, CLIP_H))
        if qplan:
            zooms = [style.Zoom(z.t0 + q0, z.t1 + q0, z.factor, z.ax, z.ay, z.kind, z.ramp_in, z.ramp_out)
                     for z in qplan.zooms]
            face = [(a + q0, b + q0) for a, b in qplan.facecam]
            face_px = qplan.facecam_px
    ass = _write_ass(cfg, words, work, f"ev{idx:02d}")

    def png_in(p: Path) -> int:
        inputs.append(["-loop", "1", "-framerate", str(fps), "-t", f"{total:.2f}", "-i", str(p)])
        return len(inputs) - 1

    cur = "cv"
    if face and face_px:
        cw, ch, cx, cy = face_px
        parts.append("[cv]split=2[cvm][cvf]")
        parts.append(f"[cvf]crop={cw}:{ch}:{cx}:{cy},scale={W}:{CLIP_H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{CLIP_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[cvfc]")
        parts.append(f"[cvm]scale={W}:{CLIP_H},setsar=1[cvs]")
        enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in face)
        parts.append(f"[cvs][cvfc]overlay=0:0:enable='{enable}'[cvo]")
        cur = "cvo"
    if zooms:
        parts.append(effects.zoom_windows_filter(cur, "cvz", zooms, (W, CLIP_H), fps))
        cur = "cvz"
    parts.append(_composite(cur, "comp", fps, "k"))
    cur = "comp"
    ib, ia = png_in(png_before), png_in(png_after)
    parts.append(f"[{cur}][{ib}:v]overlay=0:0:enable='lt(t,{kw_t:.3f})'[o1]")
    parts.append(f"[o1][{ia}:v]overlay=0:0:enable='gte(t,{kw_t:.3f})'[o2]")
    cur = "o2"
    if png_pop:
        ip = png_in(png_pop)
        parts.append(f"[{cur}][{ip}:v]overlay=0:0:enable='between(t,{kw_t:.3f},{kw_t + 0.35:.3f})'[o3]")
        cur = "o3"
    if impact_t is not None:
        ii = png_in(impact_png(work / "impacto.png"))
        parts.append(f"[{cur}][{ii}:v]overlay=0:0:enable='between(t,{impact_t:.3f},{impact_t + 0.1:.3f})'[o4]")
        cur = "o4"
    if ctx.perfil:
        ipf = png_in(ctx.perfil)
        parts.append(f"[{cur}][{ipf}:v]overlay=0:0[o5]")
        cur = "o5"
    color = FLASH.get(ev.get("tipo_evento", "otro"), FLASH["otro"])
    parts.append(f"[{cur}]drawbox=x=0:y=0:w=iw:h=ih:color={color}@0.55:t=fill:"
                 f"enable='between(t,{kw_t:.3f},{kw_t + 2.0 / fps:.3f})'[o6]")
    cur, cwd = "o6", None
    if ass:
        parts.append(f"[{cur}]subtitles=f={ass}:fontsdir=fonts[o7]")
        cur, cwd = "o7", work
    parts.append(f"[{cur}]fps={fps},format=yuv420p[vout]")
    parts.append("[0:a]aresample=48000,aformat=channel_layouts=stereo,apad[nar];"
                 "[ca]aformat=channel_layouts=stereo[cas];[cas][nar]amix=inputs=2:duration=first:normalize=0[amix]")
    parts.append(_audio_master("amix", "aout"))
    out = work / f"{len(ctx.pieces) + 1:03d}_ev{idx:02d}.mp4"
    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for args in inputs:
        cmd += args
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]", *encode_args(cfg),
            "-t", f"{total:.3f}", str(out)]
    tools.run(cmd, timeout=max(300, total * 20), cwd=cwd)
    ctx.pieces.append(Piece(out, total))
    return after


def _still_piece(ctx: Ctx, out: Path, still: Path, dur: float, wav: Path | None, words: list, overlays: list,
                 *, reveal: bool = False, kenburns: bool = True, flashes: list[tuple[float, str]] = (),
                 closeup: tuple[int, int, int, int] | None = None, arrow: Path | None = None,
                 fade_out: bool = False, name: str = "x") -> None:
    """Pieza armada sobre una foto del día (intro y cierre)."""
    fps = ctx.fps
    frames = max(1, int(dur * fps))
    inputs = [["-loop", "1", "-framerate", str(fps), "-t", f"{dur + 0.2:.2f}", "-i", str(still)]]
    if wav:
        inputs.append(["-i", str(wav)])
    else:
        inputs.append(["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo"])
    parts = ["[0:v]split=2[st][stc]"]
    z = f"1+0.10*on/{frames}" if kenburns else "1"
    parts.append(f"[st]scale={W}:{CLIP_H}:force_original_aspect_ratio=increase,crop={W}:{CLIP_H},setsar=1,"
                 f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{CLIP_H}:fps={fps},"
                 f"setpts=N/({fps}*TB)[kb]")
    parts.append(_composite("kb", "comp", fps, "s"))
    cur = "comp"
    if closeup:
        cw, ch, cx, cy = closeup
        parts.append(f"[stc]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},setsar=1,fps={fps}[cu]")
        parts.append(f"[{cur}][cu]overlay=0:0:enable='lt(t,0.25)'[cuo]")
        cur = "cuo"
    else:
        parts.append("[stc]nullsink")
    if reveal:
        parts.append(f"color=c=black:s={W}x{H}:r={fps}:d=0.7,format=yuv420p,setsar=1[blk];"
                     f"[{cur}]format=yuv420p,setsar=1[rv]")
        parts.append("[blk][rv]xfade=transition=pixelize:duration=0.6:offset=0.05[rev]")
        cur = "rev"
    for k, (png, enable) in enumerate(overlays):
        inputs.append(["-loop", "1", "-framerate", str(fps), "-t", f"{dur + 0.2:.2f}", "-i", str(png)])
        parts.append(f"[{cur}][{len(inputs) - 1}:v]overlay=0:0:enable='{enable}'[ov{k}]")
        cur = f"ov{k}"
    if arrow:
        # Flecha que se mueve hacia el botón de seguir (columna derecha de TikTok).
        inputs.append(["-loop", "1", "-framerate", str(fps), "-t", f"{dur + 0.2:.2f}", "-i", str(arrow)])
        parts.append(f"[{cur}][{len(inputs) - 1}:v]overlay=x='{int(W * 0.62)}+40*sin(2*PI*1.6*t)':"
                     f"y={int(H * 0.47)}:enable='gte(t,0.3)'[arw]")
        cur = "arw"
    for k, (t, color) in enumerate(flashes):
        parts.append(f"[{cur}]drawbox=x=0:y=0:w=iw:h=ih:color={color}@0.5:t=fill:"
                     f"enable='between(t,{t:.3f},{t + 2.0 / fps:.3f})'[fl{k}]")
        cur = f"fl{k}"
    cwd = None
    ass = _write_ass(ctx.cfg, words, ctx.work, name) if words else None
    if ass:
        parts.append(f"[{cur}]subtitles=f={ass}:fontsdir=fonts[sub]")
        cur, cwd = "sub", ctx.work
    fade = f",fade=t=out:st={max(0.0, dur - 3.0):.3f}:d=3" if fade_out else ""
    parts.append(f"[{cur}]fps={fps},trim=duration={dur:.3f},setpts=PTS-STARTPTS{fade},format=yuv420p[vout]")
    parts.append("[1:a]aresample=48000,aformat=channel_layouts=stereo,apad[apd]")
    parts.append(_audio_master("apd", "aout", fade_out_at=max(0.0, dur - 3.0) if fade_out else None))
    cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for args in inputs:
        cmd += args
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]", *encode_args(ctx.cfg),
            "-t", f"{dur:.3f}", str(out)]
    tools.run(cmd, timeout=300, cwd=cwd)
    ctx.pieces.append(Piece(out, dur))


# ---------------------------------------------------------------------------
# Resumen completo
# ---------------------------------------------------------------------------
def fit_to_limit(cfg: dict, events: list[dict], durations: list[float], fixed: float) -> list[int]:
    """Índices de los hechos que caben en el máximo: se quitan primero los de menor prioridad."""
    max_s = float(cfg["edicion"].get("resumen_tiktok_max_s", 240))
    keep = list(range(len(events)))
    while keep and fixed + sum(durations[i] for i in keep) > max_s:
        drop = min(keep, key=lambda i: (events[i].get("prioridad", 3), -i))
        keep.remove(drop)
    return keep


def render_recap(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                 progress=None) -> Path | None:
    events = [e for e in decision.get("resumen_tiktok") or [] if e.get("narracion")]
    if not events or not narrator.available(cfg):
        return None
    fecha = session["fecha"]
    folder = session_dir(cfg, fecha)
    work = folder / "render_tiktok"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    by_id = {int(c["id"]): c for c in candidates}
    counters = decision.get("tiktok_contadores") or []
    ctx = Ctx(cfg, db, session, work, counters, int(cfg["edicion"]["fps"]),
              perfil=perfil_png(PERFIL, work / "perfil.png") if PERFIL.exists() else None)
    try:
        # Narraciones primero: su duración decide qué hechos entran en el máximo.
        intro = narrator.narrate(cfg, decision.get("tiktok_intro") or cfg["evento"]["nombre"], work / "intro.wav")
        outro = narrator.narrate(cfg, decision.get("tiktok_cierre") or "Sígueme para no perderte el próximo.",
                                 work / "cierre.wav")
        est = [len(e["narracion"].split()) / 2.5 + max(0.0, e.get("cita_fin", 0) - e.get("cita_inicio", 0))
               for e in events]
        chosen = [events[i] for i in fit_to_limit(cfg, events, est, intro.dur + outro.dur + 2.0)]
        values = {c["id"]: int(c.get("inicial", 0)) for c in counters}
        final = dict(values)
        for e in chosen:
            if e.get("contador"):
                final[e["contador"]] = final.get(e["contador"], 0) + int(e.get("suma") or 0)

        # Foto del día (el hecho de mayor prioridad) para la intro y el cierre.
        top = max(chosen, key=lambda e: e.get("prioridad", 3)) if chosen else events[0]
        cand_top = by_id.get(int(top["candidato_id"]))
        still = work / "foto.jpg"
        loc = None
        if cand_top:
            t_top = cand_top["start_ts"] + (top.get("momento_clave") or top["inicio"])
            loc = locate_range(db, session["id"], cand_top["slug"], t_top - 1.0, t_top + 1.0,
                               float(cfg["grabacion"]["desfase_chat_s"]))
        if loc:
            snapshot_from_file(loc[0], loc[1] + min(1.0, loc[2] / 2), still)

        # Intro: revelado pixel, logo, foto con paneo-zoom, contadores grandes -> esquinas, flashes en cifras.
        if progress:
            progress("resumen TikTok: intro")
        dur_i = intro.dur + 0.35
        overlays = []
        if LOGO.exists():
            logo = work / "logo_full.png"
            lg = Image.open(LOGO).convert("RGBA")
            lg.thumbnail((int(W * 0.6), int(H * 0.25)))
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            canvas.alpha_composite(lg, ((W - lg.width) // 2, (H - lg.height) // 2))
            canvas.save(logo)
            overlays.append((logo, "between(t,0.3,1.8)"))
        overlays.append((counters_png(counters, final, work / "grandes.png", big=True),
                         f"between(t,{1.0 if not LOGO.exists() else 1.9},{max(1.2, dur_i - 0.6):.2f})"))
        overlays.append((counters_png(counters, values, work / "c_ini.png"), f"gte(t,{max(1.2, dur_i - 0.6):.2f})"))
        if ctx.perfil:
            overlays.append((ctx.perfil, "1"))
        flashes = [(a, "0xFFD400") for a, _b, w in intro.words
                   if re.search(r"\d", w) or normalize(w).strip(".,") in NUMEROS]
        if still.exists():
            _still_piece(ctx, work / "000_intro.mp4", still, dur_i, intro.wav, intro.words, overlays,
                         reveal=True, flashes=flashes, name="intro")

        # Hechos.
        for i, ev in enumerate(chosen):
            cand = by_id.get(int(ev["candidato_id"]))
            if not cand:
                continue
            if progress:
                progress(f"resumen TikTok: hecho {i + 1}/{len(chosen)}")
            try:
                values = render_event(ctx, i, ev, cand, values)
            except Exception as exc:  # noqa: BLE001 - un hecho roto no tumba el resumen
                log.error("Falló el hecho %d del resumen TikTok: %s", i + 1, exc)

        # Cierre: flash, primerísimo plano 0.25 s, "SÍGUEME" con flecha y fade de 3 s.
        if progress:
            progress("resumen TikTok: cierre")
        dur_o = max(outro.dur + 0.6, 3.5)
        closeup = None
        st = get_streamer(cfg, cand_top["slug"]) if cand_top else None
        if st and st.get("camara") and still.exists():
            iw, ih = Image.open(still).size
            cam = st["camara"]
            closeup = (int(cam["w"] * iw) // 2 * 2, int(cam["h"] * ih) // 2 * 2,
                       int(cam["x"] * iw) // 2 * 2, int(cam["y"] * ih) // 2 * 2)
        cta = [(text_png("Sígueme", work / "sigueme.png", 150), "gte(t,0.25)")]
        if still.exists():
            _still_piece(ctx, work / "999_cierre.mp4", still, dur_o, outro.wav, outro.words, cta, kenburns=False,
                         flashes=[(0.0, "0xFFFFFF")], closeup=closeup, arrow=arrow_png(work / "flecha.png"),
                         fade_out=True, name="cierre")
        if not any("_ev" in p.path.name for p in ctx.pieces):
            log.error("No se pudo renderizar ningún hecho del resumen TikTok narrado")
            return None
        out = folder / f"resumen_tiktok_{fecha}.mp4"
        concat_pieces([p.path for p in ctx.pieces], out)
    finally:
        if not log.isEnabledFor(logging.DEBUG):
            shutil.rmtree(work, ignore_errors=True)
    total = tools.probe_duration(out)
    db.add_output(session["id"], "resumen_tiktok", str(out), {"segundos": round(total, 1), "narrado": True})
    log.info("Resumen TikTok narrado listo: %s (%.0f s, %d piezas)", out.name, total, len(ctx.pieces))
    return out
