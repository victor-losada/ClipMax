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
