"""Gráficos con Pillow: tarjetas de narración y títulos sobre los clips.

Se generan como PNG y ffmpeg los usa como una entrada más. Así evitamos el
filtro drawtext, cuyo escapado de rutas y texto en Windows es un dolor de cabeza.
"""

from __future__ import annotations

import logging
import re
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import resolve_path

log = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/verdanab.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
ACCENT = (83, 252, 24)  # verde Kick


@lru_cache(maxsize=32)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = ([path] if path else []) + _FONT_CANDIDATES
    for c in candidates:
        p = resolve_path(c) if c else None
        if p and p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    if sys.platform == "win32":
        log.warning("No encontré fuentes TrueType; uso la fuente por defecto de Pillow")
    return ImageFont.load_default(size=size)


def _strip_unrenderable(text: str) -> str:
    # Emojis y símbolos fuera del plano básico salen como cuadros en fuentes normales.
    return re.sub(r"[\U00010000-\U0010FFFF]", "", text or "").strip()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        line = ""
        for w in words:
            test = f"{line} {w}".strip()
            if draw.textlength(test, font=font) <= max_width or not line:
                line = test
            else:
                lines.append(line)
                line = w
        if line:
            lines.append(line)
    return lines


def _background(size: tuple[int, int], bg_image: Path | None) -> Image.Image:
    w, h = size
    if bg_image and Path(bg_image).exists():
        img = Image.open(bg_image).convert("RGB")
        scale = max(w / img.width, h / img.height)
        img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
        left, top = (img.width - w) // 2, (img.height - h) // 2
        img = img.crop((left, top, left + w, top + h)).filter(ImageFilter.GaussianBlur(radius=max(8, w // 60)))
        dark = Image.new("RGB", size, (0, 0, 0))
        return Image.blend(img, dark, 0.55)
    # Degradado oscuro si no hay imagen.
    img = Image.new("RGB", size, (12, 14, 18))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        c = int(12 + 20 * y / h)
        draw.line([(0, y), (w, y)], fill=(c, c + 2, c + 6))
    return img


def render_card(text: str, size: tuple[int, int], out_png: Path, *, bg_image: Path | None = None,
                label: str = "", font_path: str = "", big: bool = False) -> Path:
    """Tarjeta de narración: fondo desenfocado + texto centrado + etiqueta del evento."""
    w, h = size
    img = _background(size, bg_image)
    draw = ImageDraw.Draw(img)
    base = min(w, h)
    text = _strip_unrenderable(text)
    size_px = int(base * (0.085 if big else 0.058))
    font = _font(font_path, size_px)
    max_width = int(w * 0.82)
    lines = _wrap(draw, text, font, max_width)
    # Si no cabe, reducimos la fuente.
    while len(lines) * size_px * 1.3 > h * 0.7 and size_px > 18:
        size_px = int(size_px * 0.9)
        font = _font(font_path, size_px)
        lines = _wrap(draw, text, font, max_width)
    line_h = int(size_px * 1.3)
    y = (h - line_h * len(lines)) // 2
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = (w - tw) // 2
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h
    if label:
        lf = _font(font_path, int(base * 0.032))
        lbl = _strip_unrenderable(label).upper()
        pad = int(base * 0.02)
        tw = draw.textlength(lbl, font=lf)
        box = (pad, pad, pad * 3 + int(tw), pad * 2 + int(base * 0.045))
        draw.rectangle(box, fill=ACCENT)
        draw.text((pad * 2, pad + int(base * 0.006)), lbl, font=lf, fill=(0, 0, 0))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


def render_lower_third(title: str, streamer: str, size: tuple[int, int], out_png: Path,
                       font_path: str = "") -> Path:
    """PNG transparente del tamaño del video con el título del clip abajo a la izquierda."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    base = min(w, h)
    title = _strip_unrenderable(title).upper()
    streamer = _strip_unrenderable(streamer).upper()
    tf = _font(font_path, int(base * 0.055))
    sf = _font(font_path, int(base * 0.032))
    pad = int(base * 0.022)
    max_w = int(w * 0.8)
    lines = _wrap(draw, title, tf, max_w - 2 * pad) if title else []
    title_h = int(base * 0.055 * 1.25) * len(lines)
    tag_h = int(base * 0.032 * 1.5)
    box_w = max([draw.textlength(ln, font=tf) for ln in lines] + [draw.textlength(streamer, font=sf)]) + 2 * pad
    x0 = int(w * 0.04)
    y1 = int(h * (0.86 if h > w else 0.9))
    y0 = y1 - title_h - tag_h - 2 * pad
    draw.rectangle((x0, y0, x0 + int(box_w), y1), fill=(0, 0, 0, 185))
    draw.rectangle((x0, y0, x0 + int(base * 0.008), y1), fill=(*ACCENT, 255))
    ty = y0 + pad // 2
    if streamer:
        draw.text((x0 + pad, ty), streamer, font=sf, fill=(*ACCENT, 255))
    ty += tag_h
    for line in lines:
        draw.text((x0 + pad, ty), line, font=tf, fill=(255, 255, 255, 255))
        ty += int(base * 0.055 * 1.25)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


def render_split_tags(main: str, partner: str, size: tuple[int, int], out_png: Path,
                      font_path: str = "") -> Path:
    """Nombre de cada streamer en su mitad de la pantalla dividida."""
    w, h = size
    vertical = h > w
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    base = min(w, h)
    font = _font(font_path, int(base * 0.038))
    pad = int(base * 0.018)
    origins = [(pad, pad), (pad, h // 2 + pad)] if vertical else [(pad, pad), (w // 2 + pad, pad)]
    for (x, y), name in zip(origins, (main, partner)):
        label = _strip_unrenderable(name).upper()
        tw = draw.textlength(label, font=font)
        box = (x, y, x + int(tw) + 2 * pad, y + int(base * 0.038 * 1.2) + pad)
        draw.rounded_rectangle(box, radius=pad // 2, fill=(0, 0, 0, 170))
        draw.rectangle((x, y, x + max(3, pad // 3), box[3]), fill=(*ACCENT, 255))
        draw.text((x + pad, y + pad // 3), label, font=font, fill=(255, 255, 255, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


# ---------------------------------------------------------------------------
# Estilo "Eufonía": fuente pixel para rótulos, sting y pantalla final; redondeada para narración
# ---------------------------------------------------------------------------
_SIN_TILDE = str.maketrans("ÁÉÍÓÚÜáéíóúü", "AEIOUUaeiouu")


def _pixel_text(text: str) -> str:
    """La fuente pixel no trae vocales con tilde en mayúscula: se quitan las tildes."""
    return _strip_unrenderable(text).translate(_SIN_TILDE).upper()


def _style_fonts() -> tuple[str, str]:
    from .style import CAPTION_FONT, PIXEL_FONT

    return str(PIXEL_FONT) if PIXEL_FONT.exists() else "", str(CAPTION_FONT) if CAPTION_FONT.exists() else ""


def _gradient_text(img: Image.Image, xy: tuple[int, int], text: str, font, top: tuple[int, int, int],
                   bottom: tuple[int, int, int], stroke: int) -> None:
    """Texto con relleno degradado vertical y contorno oscuro (pixel art de la pantalla final)."""
    draw = ImageDraw.Draw(img)
    draw.text(xy, text, font=font, fill=(0, 0, 0, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)
    x0, y0, x1, y1 = mask.getbbox() or (0, 0, img.width, img.height)
    grad = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(y0, y1 + 1):
        k = (y - y0) / max(1, y1 - y0)
        gd.line([(x0, y), (x1, y)], fill=(*[int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)], 255))
    img.paste(grad, (0, 0), mask)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, path: str, size_px: int, max_width: int):
    font = _font(path, size_px)
    while draw.textlength(text, font=font) > max_width and size_px > 10:
        size_px = int(size_px * 0.9)
        font = _font(path, size_px)
    return font


def render_pixel_label(text: str, size: tuple[int, int], out_png: Path) -> Path:
    """Rótulo de desenlace: "ETIQUETA · NOMBRE" en pixel blanco con contorno, abajo a la derecha."""
    pixel, _round = _style_fonts()
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = _pixel_text(text)
    font = _fit_font(draw, text, pixel, int(min(w, h) * 0.034), int(w * 0.6))
    tw = draw.textlength(text, font=font)
    x, y = int(w - tw - w * 0.04), int(h * 0.86)
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), stroke_width=max(3, font.size // 6),
              stroke_fill=(0, 0, 0, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


def render_sting_title(title: str, subtitle: str, size: tuple[int, int], out_png: Path) -> Path:
    """Título del sting (después del gancho): pixel grande con degradado, centrado, fondo transparente."""
    pixel, _round = _style_fonts()
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    title = _pixel_text(title)
    font = _fit_font(draw, title, pixel, int(min(w, h) * 0.085), int(w * 0.86))
    tw = draw.textlength(title, font=font)
    y = int(h * 0.42)
    _gradient_text(img, (int((w - tw) / 2), y), title, font, (255, 236, 64), (255, 120, 20), max(4, font.size // 7))
    if subtitle:
        sub = _pixel_text(subtitle)
        sf = _fit_font(draw, sub, pixel, int(min(w, h) * 0.03), int(w * 0.8))
        sw = draw.textlength(sub, font=sf)
        draw.text((int((w - sw) / 2), y + int(font.size * 1.6)), sub, font=sf, fill=(255, 255, 255, 255),
                  stroke_width=max(2, sf.size // 6), stroke_fill=(0, 0, 0, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


def render_end_card(text: str, subtitle: str, size: tuple[int, int], out_png: Path,
                    bg_image: Path | None = None) -> Path:
    """Pantalla final: fondo (último plano desenfocado) + texto pixel con degradado."""
    pixel, _round = _style_fonts()
    w, h = size
    img = _background(size, bg_image).convert("RGBA")
    draw = ImageDraw.Draw(img)
    text = _pixel_text(text)
    font = _fit_font(draw, text, pixel, int(min(w, h) * 0.1), int(w * 0.88))
    tw = draw.textlength(text, font=font)
    y = int(h * 0.40)
    _gradient_text(img, (int((w - tw) / 2), y), text, font, (120, 255, 90), (20, 170, 255), max(5, font.size // 7))
    if subtitle:
        sub = _pixel_text(subtitle)
        sf = _fit_font(draw, sub, pixel, int(min(w, h) * 0.032), int(w * 0.8))
        sw = draw.textlength(sub, font=sf)
        draw.text((int((w - sw) / 2), y + int(font.size * 1.7)), sub, font=sf, fill=(255, 255, 255, 255),
                  stroke_width=max(2, sf.size // 6), stroke_fill=(0, 0, 0, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_png)
    return out_png


def render_card_eufonia(text: str, size: tuple[int, int], out_png: Path, *, bg_image: Path | None = None,
                        label: str = "") -> Path:
    """Tarjeta de narración del estilo nuevo: texto en fuente redondeada con contorno y la etiqueta en pixel."""
    pixel, rounded = _style_fonts()
    w, h = size
    img = _background(size, bg_image).convert("RGBA")
    draw = ImageDraw.Draw(img)
    base = min(w, h)
    text = _strip_unrenderable(text)
    size_px = int(base * 0.07)
    font = _font(rounded, size_px)
    lines = _wrap(draw, text, font, int(w * 0.8))
    while len(lines) * size_px * 1.25 > h * 0.66 and size_px > 18:
        size_px = int(size_px * 0.9)
        font = _font(rounded, size_px)
        lines = _wrap(draw, text, font, int(w * 0.8))
    line_h = int(size_px * 1.25)
    y = (h - line_h * len(lines)) // 2
    for line in lines:
        tw = draw.textlength(line, font=font)
        draw.text(((w - tw) // 2, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=max(3, size_px // 10), stroke_fill=(0, 0, 0, 255))
        y += line_h
    if label:
        lbl = _pixel_text(label)
        lf = _fit_font(draw, lbl, pixel, int(base * 0.026), int(w * 0.5))
        draw.text((int(base * 0.04), int(base * 0.04)), lbl, font=lf, fill=(255, 214, 0, 255),
                  stroke_width=max(2, lf.size // 6), stroke_fill=(0, 0, 0, 255))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_png)
    return out_png
