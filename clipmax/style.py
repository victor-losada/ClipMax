"""Director de efectos: decide DÓNDE mirar en cada tramo (estilo "Eufonía" y clips verticales).

Gramática (docs/ESTILO_EDICION.md):
- Punch-in al facecam, anclado a su esquina, 1.3-1.5x durante 2-4 s, en el momento de una
  emoción: queja, susto, rabia, risa, sorpresa, grito. Nunca sobre el juego ni en pausas.
- Zoom al texto que PROVOCA la reacción: si el streamer lee un mensaje del chat, reacciona a
  un aviso del juego (una muerte, una eliminación) o dice "miren el chat", primero se acerca
  a ese texto (~2 s) y después va la reacción en la cara.
- En la reacción más fuerte de un tramo, la cara a pantalla completa 3-6 s.
- Remate de un monólogo: zoom continuo ~4 s hasta 2x que termina en el corte.
- Captions de 1-4 palabras, literales y sincronizados, solo en frases clave o gritadas, con
  un color fijo por streamer.

Las marcas salen de cuatro fuentes: Claude (lee la transcripción: la señal más inteligente),
el volumen (gritos), un léxico de exclamaciones y la coincidencia entre lo que se dice y los
mensajes del chat (cuando el streamer lee uno).
"""

from __future__ import annotations

import array
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import effects, tools
from .config import PROJECT_ROOT, get_streamer, pair_slugs
from .mentions import normalize

log = logging.getLogger(__name__)

FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
CAPTION_FONT = FONTS_DIR / "TitanOne-Regular.ttf"      # redondeada y gruesa (captions)
PIXEL_FONT = FONTS_DIR / "PressStart2P-Regular.ttf"    # pixel (rótulos, sting, pantalla final)

# Colores de captions (#RRGGBB): la pareja principal primero (amarillo, naranja), luego el resto.
PALETTE = ["#FFD400", "#FF8A1F", "#FF4FA3", "#5CFF5C", "#3FE0FF", "#B98CFF", "#FF5C5C", "#FFFFFF"]

# Bloques del resumen y lo que se permite en cada uno.
BLOQUES = ["gancho", "premisa", "cuerpo", "subida", "pausa", "climax", "desenlace", "cierre"]
SIN_PUNCH = {"pausa", "cierre"}
SIN_CAPTIONS = {"climax", "pausa", "cierre"}
# Volumen objetivo por bloque (LUFS): la energía sube hacia el clímax y baja en el cierre.
VOLUMEN = {"gancho": -14.0, "premisa": -16.5, "cuerpo": -16.5, "subida": -15.0, "pausa": -17.5,
           "climax": -13.0, "desenlace": -15.0, "cierre": -19.0}

# Zonas de texto en la imagen del juego (fracciones 0-1): chat de Minecraft, títulos, barra superior.
ZONAS = {
    "juego": {"x": 0.0, "y": 0.52, "w": 0.46, "h": 0.36},
    "centro": {"x": 0.15, "y": 0.25, "w": 0.70, "h": 0.30},
    "arriba": {"x": 0.25, "y": 0.0, "w": 0.50, "h": 0.18},
}

HOP = 0.05  # resolución de la envolvente de volumen


# ---------------------------------------------------------------------------
# Léxico (texto ya normalizado: minúsculas, sin tildes)
# ---------------------------------------------------------------------------
LEX_FUERTE = {
    "susto": ["ayuda", "auxilio", "corre", "corran", "que es eso", "me asuste", "casi me muero", "dios mio",
              "ay no", "ay dios", "que miedo", "me cago", "atras atras"],
    "queja": ["no puede ser", "me mato", "me mataron", "me robaron", "que injusto", "no es justo", "injusto",
              "trampa", "tramposo", "que pereza", "no me jodas", "por que a mi", "esto es un robo"],
    "rabia": ["hijueputa", "hijo de puta", "malparido", "gonorrea", "carajo", "mierda", "maldito", "maldita",
              "maldita sea", "hp", "cabron", "puta madre", "me tiene mamado", "estoy mamado"],
    "risa": ["jajaja", "jajajaja", "jajajajaja", "jsjsjs", "ajajaja"],
    "sorpresa": ["no jodas", "que paso", "mentira", "en serio", "no lo puedo creer", "que que", "como asi",
                 "no puede", "ome que", "uy no"],
}
LEX_DEBIL = {"no", "que", "ay", "uy", "wow", "ome", "parce", "marica", "noo", "nooo", "mk", "uff", "oe", "jueputa"}
CUES_CHAT = ["miren el chat", "mira el chat", "lean el chat", "el chat dice", "que dice el chat", "me escribieron",
             "me escribio", "dice el chat", "lei en el chat", "vi en el chat", "leo el chat", "chat dice"]
CUES_JUEGO = ["murio", "lo mataron", "la mataron", "mataron a", "eliminado", "eliminada", "eliminaron",
              "fue asesinado", "esta muerto", "salio en el chat", "chat del juego", "se murio"]
CUES_ARRIBA = ["miren arriba", "la barra", "cuenta regresiva", "cuenta atras", "el temporizador", "el titulo"]


@dataclass
class Mark:
    t: float                 # segundos en la línea de tiempo del tramo (antes de quitar silencios)
    kind: str                # queja|susto|rabia|risa|sorpresa|grito|remate|texto
    strength: float          # 0-1
    source: str = ""         # claude|voz|lexico|chat|pista
    zona: str = ""           # solo "texto": chat|juego|centro|arriba
    text: str = ""


@dataclass
class Zoom:
    t0: float
    t1: float
    factor: float
    ax: float                # ancla (0-1): el punto que queda fijo en pantalla
    ay: float
    kind: str = "punch"      # punch|texto|final|fijo
    ramp_in: float = 0.12
    ramp_out: float = 0.22


@dataclass
class Plan:
    zooms: list[Zoom] = field(default_factory=list)
    facecam: list[tuple[float, float]] = field(default_factory=list)   # cara a pantalla completa
    facecam_px: tuple[int, int, int, int] | None = None                # recorte (w, h, x, y) de la cámara en el original
    captions: list[tuple[float, float, str]] | None = None             # None = subtítulos continuos
    color: str = "#FFD400"
    marks: list[Mark] = field(default_factory=list)                    # para depurar / pruebas


# ---------------------------------------------------------------------------
# Geometría: dónde queda cada cosa del stream en el cuadro de salida
# ---------------------------------------------------------------------------
class Geometry:
    """Traduce recuadros de la imagen original (0-1) a la imagen de salida (0-1) según el encuadre
    que arma editor.layout_filter."""

    def __init__(self, src_size: tuple[int, int], out_size: tuple[int, int], modo: str, camara: dict | None):
        self.iw, self.ih = src_size
        self.w, self.h = out_size
        self.modo, self.camara = modo, camara
        self.vertical = self.h > self.w
        self.stack = self.vertical and bool(camara) and modo != "cara"
        if modo == "cara":
            self.game = None
        elif self.stack:
            top = (int(self.h * 0.38) // 2 * 2) / self.h
            gh = self.w * self.ih / self.iw / self.h            # alto del juego (fracción de la salida)
            area = 1.0 - top
            gh = min(gh, area)
            self.game = (0.0, top + (area - gh) / 2, 1.0, gh)   # juego ajustado al ancho, centrado abajo
        else:
            s = min(self.w / self.iw, self.h / self.ih)
            gw, gh = self.iw * s / self.w, self.ih * s / self.h
            self.game = ((1 - gw) / 2, (1 - gh) / 2, gw, gh)

    def from_source(self, box: dict) -> tuple[float, float, float, float] | None:
        """Recuadro de la imagen original -> (x, y, w, h) en la salida, o None si no se ve."""
        if not self.game:
            return None
        gx, gy, gw, gh = self.game
        return gx + box["x"] * gw, gy + box["y"] * gh, box["w"] * gw, box["h"] * gh

    def cam(self) -> tuple[float, float, float, float] | None:
        if not self.camara:
            return None
        if self.modo == "cara":
            return 0.0, 0.0, 1.0, 1.0
        if self.stack:
            return 0.0, 0.0, 1.0, (int(self.h * 0.38) // 2 * 2) / self.h
        return self.from_source(self.camara)

    def cam_anchor(self) -> tuple[float, float]:
        """Esquina de la cámara: al hacer zoom anclado ahí, la cara crece sin salirse del cuadro."""
        cam = self.cam()
        if not cam:
            return 0.5, 0.5
        x, y, w, h = cam
        cx, cy = x + w / 2, y + h / 2

        def side(c: float) -> float:
            return 0.0 if c < 0.4 else 1.0 if c > 0.6 else 0.5
        return side(cx), side(cy)

    def cam_px(self) -> tuple[int, int, int, int] | None:
        """Recorte (w, h, x, y) de la cámara en la imagen ORIGINAL (la cara completa sale de ahí,
        con toda su resolución y sin los recortes del encuadre vertical)."""
        if not self.camara:
            return None
        c = self.camara
        pw, ph = max(2, int(c["w"] * self.iw) // 2 * 2), max(2, int(c["h"] * self.ih) // 2 * 2)
        px, py = int(c["x"] * self.iw) // 2 * 2, int(c["y"] * self.ih) // 2 * 2
        return min(pw, self.iw - px), min(ph, self.ih - py), px, py


def zoom_to_box(box: tuple[float, float, float, float], max_factor: float = 2.6) -> tuple[float, float, float]:
    """(factor, ax, ay) para que el recuadro ocupe ~85 % del cuadro y quede centrado."""
    x, y, w, h = box
    f = max(1.3, min(max_factor, 0.85 / max(w, h, 0.05)))
    cx, cy = x + w / 2, y + h / 2

    def anchor(c: float) -> float:
        return min(1.0, max(0.0, (c - 0.5 / f) / (1 - 1 / f)))
    return f, anchor(cx), anchor(cy)


# ---------------------------------------------------------------------------
# Señales
# ---------------------------------------------------------------------------
def loudness(path: str, start: float, dur: float) -> list[float]:
    """Volumen (dBFS) cada HOP segundos del tramo."""
    proc = tools.run([tools.ffmpeg(), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}",
                      "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                     timeout=max(60, dur * 2), check=False)
    raw = proc.stdout or b""
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) // 2 * 2])
    hop = int(8000 * HOP)
    out = []
    for i in range(0, len(pcm) - hop + 1, hop):
        chunk = pcm[i:i + hop]
        rms = math.sqrt(sum(v * v for v in chunk) / hop) / 32768.0
        out.append(20 * math.log10(rms + 1e-6))
    return out


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


def shout_marks(env: list[float]) -> list[Mark]:
    """Gritos: tramos claramente más fuertes que lo normal del mismo clip."""
    if len(env) < 40:
        return []
    med = _median(env)
    mad = _median([abs(v - med) for v in env]) or 1.0
    thr = max(med + max(7.0, 3.0 * mad), -30.0)
    marks: list[Mark] = []
    i = 0
    while i < len(env):
        if env[i] < thr:
            i += 1
            continue
        j = i
        while j < len(env) and env[j] >= thr - 2.0:
            j += 1
        if (j - i) * HOP >= 0.25:
            peak = max(range(i, j), key=lambda k: env[k])
            excess = env[peak] - med
            strength = max(0.3, min(1.0, (excess - 7.0) / 14.0 + 0.45))
            t = i * HOP
            if not marks or t - marks[-1].t >= 1.5:
                marks.append(Mark(t, "grito", strength, "voz"))
            elif strength > marks[-1].strength:
                marks[-1] = Mark(t, "grito", strength, "voz")
        i = j
    return marks


def _norm_words(words: list[tuple[float, float, str]]) -> list[tuple[float, float, str, str]]:
    """(inicio, fin, palabra normalizada, palabra original) sin las que quedan vacías."""
    return [(a, b, normalize(w), w) for a, b, w in words if normalize(w)]


def lexicon_marks(words: list[tuple[float, float, str]], env: list[float]) -> list[Mark]:
    """Exclamaciones, insultos, risa y repeticiones ("no no no") en lo que se dice."""
    nw = _norm_words(words)
    toks = [x[2] for x in nw]
    orig = [x[3] for x in nw]
    med = _median(env) if env else 0.0

    def loud_at(t: float) -> bool:
        if not env:
            return False
        i0, i1 = int(max(0.0, t - 0.3) / HOP), int((t + 0.8) / HOP) + 1
        seg = env[i0:i1]
        return bool(seg) and max(seg) > med + 5.0

    marks: list[Mark] = []
    for i in range(len(nw)):
        for n in (4, 3, 2, 1):
            phrase = " ".join(toks[i:i + n])
            if len(toks[i:i + n]) < n:
                continue
            kind = next((k for k, lst in LEX_FUERTE.items() if phrase in lst), None)
            if kind:
                marks.append(Mark(nw[i][0], kind, 0.6 + (0.2 if loud_at(nw[i][0]) else 0.0), "lexico",
                                  text=" ".join(orig[i:i + n])))
                break
        else:
            t = toks[i]
            if re.fullmatch(r"(ja){3,}j?|(je){3,}|(js){3,}", t):
                marks.append(Mark(nw[i][0], "risa", 0.6, "lexico", text=orig[i]))
            elif t in LEX_DEBIL and loud_at(nw[i][0]):
                marks.append(Mark(nw[i][0], "sorpresa", 0.5, "lexico", text=orig[i]))
        # Repeticiones: "no no no", "corre corre corre"
        if i + 2 < len(toks) and toks[i] == toks[i + 1] == toks[i + 2] and len(toks[i]) <= 8:
            marks.append(Mark(nw[i][0], "susto" if toks[i] in ("corre", "ayuda", "atras") else "sorpresa", 0.7,
                              "lexico", text=" ".join(orig[i:i + 3])))
    return marks


def cue_marks(words: list[tuple[float, float, str]], has_chat_zone: bool) -> list[Mark]:
    """Frases que señalan un texto en pantalla: "miren el chat", "mataron a...", "la cuenta regresiva"."""
    nw = _norm_words(words)
    toks = [x[2] for x in nw]
    marks: list[Mark] = []
    for i in range(len(nw)):
        for n in (4, 3, 2, 1):
            phrase = " ".join(toks[i:i + n])
            if len(toks[i:i + n]) < n:
                continue
            if phrase in CUES_CHAT and has_chat_zone:
                marks.append(Mark(nw[i][0], "texto", 0.7, "pista", zona="chat", text=phrase))
                break
            if phrase in CUES_JUEGO:
                marks.append(Mark(nw[i][0] - 0.8, "texto", 0.6, "pista", zona="juego", text=phrase))
                break
            if phrase in CUES_ARRIBA:
                marks.append(Mark(nw[i][0], "texto", 0.5, "pista", zona="arriba", text=phrase))
                break
    return marks


_STOP = {"que", "de", "la", "el", "en", "y", "a", "los", "las", "un", "una", "es", "por", "con", "no", "se",
         "lo", "le", "me", "te", "su", "mi", "al", "del", "para", "pero", "si", "ya", "muy", "mas", "como"}


def chat_read_marks(words: list[tuple[float, float, str]], chat: list[tuple[float, str, str]]) -> list[Mark]:
    """El streamer lee en voz alta un mensaje del chat: se acerca al chat justo antes de que lo lea.

    chat: (t en el tramo, usuario, texto). Coincidencia: al menos 3 palabras del mensaje (o todas,
    si tiene 2) dichas en orden poco después de que llegó el mensaje.
    """
    nw = [(a, b, w) for a, b, w, _o in _norm_words(words)]
    marks: list[Mark] = []
    last = -99.0
    for tc, user, text in chat:
        msg = [w for w in normalize(text).split() if len(w) >= 3 and w not in _STOP]
        if len(msg) < 2:
            continue
        need = len(msg) if len(msg) <= 2 else max(3, math.ceil(0.6 * len(msg)))
        cand = [(a, w) for a, _b, w in nw if tc - 3.0 <= a <= tc + 40.0]
        for i in range(len(cand)):
            if cand[i][1] not in msg[:2]:
                continue
            window = [w for _a, w in cand[i:i + len(msg) + 4]]
            pos, hits = 0, 0
            for w in msg:
                try:
                    pos = window.index(w, pos) + 1
                    hits += 1
                except ValueError:
                    continue
            if hits >= need:
                t = cand[i][0]
                if t - last > 6.0:
                    marks.append(Mark(t, "texto", 0.8, "chat", zona="chat", text=f"{user}: {text}"[:80]))
                    last = t
                break
    return marks


def claude_marks(item: dict, offset: float) -> list[Mark]:
    """Marcas que puso Claude (tiempos relativos al candidato; offset los pasa al tramo)."""
    marks: list[Mark] = []
    for e in item.get("emociones") or []:
        marks.append(Mark(float(e.get("t", 0)) + offset, str(e.get("tipo") or "sorpresa"), 0.9, "claude",
                          text=str(e.get("texto") or "")))
    for z in item.get("zoom_texto") or []:
        marks.append(Mark(float(z.get("t", 0)) + offset, "texto", 0.9, "claude", zona=str(z.get("zona") or "juego"),
                          text=str(z.get("texto") or "")))
    mom = float(item.get("momento_clave") or 0)
    if mom > 0:
        marks.append(Mark(mom + offset, "remate", 0.75, "claude"))
    return marks


def merge_marks(marks: list[Mark], window: float = 1.2) -> list[Mark]:
    """Une marcas de emoción cercanas (gana Claude y, si no, la más fuerte). Los textos van aparte."""
    texts = sorted((m for m in marks if m.kind == "texto"), key=lambda m: m.t)
    emos = sorted((m for m in marks if m.kind != "texto"), key=lambda m: m.t)
    out: list[Mark] = []
    for m in emos:
        if out and m.t - out[-1].t < window:
            prev = out[-1]
            better = (m.source == "claude") > (prev.source == "claude") or \
                ((m.source == "claude") == (prev.source == "claude") and m.strength > prev.strength)
            keep, other = (m, prev) if better else (prev, m)
            out[-1] = Mark(keep.t, keep.kind, min(1.0, max(keep.strength, other.strength) + 0.1), keep.source,
                           text=keep.text or other.text)
        else:
            out.append(m)
    merged_texts: list[Mark] = []
    for m in texts:
        if merged_texts and m.t - merged_texts[-1].t < 4.0:
            if m.source == "claude" and merged_texts[-1].source != "claude":
                merged_texts[-1] = m
            continue
        merged_texts.append(m)
    return out + merged_texts


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def _overlaps(a0: float, a1: float, spans: list[tuple[float, float]], margin: float = 0.0) -> bool:
    return any(a0 < b1 + margin and b0 < a1 + margin for b0, b1 in spans)


def streamer_color(cfg: dict, slug: str) -> str:
    st = get_streamer(cfg, slug) or {}
    if st.get("color"):
        return st["color"]
    order = pair_slugs(cfg) + [s["slug"] for s in cfg["streamers"] if s["slug"] not in pair_slugs(cfg)]
    idx = order.index(slug) if slug in order else len(order)
    return PALETTE[idx % len(PALETTE)]


def key_captions(marks: list[Mark], words_out: list[tuple[float, float, str]], length: float,
                 per_minute: float = 8.0) -> list[tuple[float, float, str]]:
    """Captions de 1-4 palabras literales, sincronizadas, en las frases clave o gritadas."""
    caps: list[tuple[float, float, str]] = []
    budget = max(1, int(per_minute * length / 60 + 1))
    for m in sorted((m for m in marks if m.kind != "texto"), key=lambda m: -m.strength):
        if len(caps) >= budget:
            break
        start = next((i for i, (a, _b, _w) in enumerate(words_out) if a >= m.t - 0.35), None)
        if start is None or words_out[start][0] - m.t > 1.2:
            continue
        group = [words_out[start]]
        for w in words_out[start + 1:start + 4]:
            if w[0] - group[-1][1] > 0.45 or len(" ".join(x[2] for x in group + [w])) > 18:
                break
            group.append(w)
        t0 = group[0][0]
        t1 = max(group[-1][1] + 0.3, t0 + 0.9)
        if any(t0 < b + 1.0 and a < t1 + 1.0 for a, b, _ in caps):
            continue
        caps.append((round(t0, 2), round(min(t1, length), 2), " ".join(x[2] for x in group)))
    return sorted(caps)


def build_plan(marks_out: list[Mark], length: float, geo: Geometry, *, bloque: str = "", zona_chat: dict | None = None,
               captions_words: list[tuple[float, float, str]] | None = None, zoom_final: bool = False,
               facecam_claude: list[tuple[float, float]] | None = None, allow_facecam: bool = True,
               color: str = "#FFD400") -> Plan:
    """Marcas (ya en la línea de tiempo de salida) -> zooms, cara completa y captions."""
    plan = Plan(color=color, marks=marks_out)
    has_cam = geo.cam() is not None
    ax, ay = geo.cam_anchor()
    busy: list[tuple[float, float]] = []

    # 1) La cara a pantalla completa: la que pidió Claude o la reacción más fuerte del tramo.
    if has_cam and allow_facecam and bloque not in SIN_PUNCH and geo.modo != "cara":
        wins = [(max(0.0, a), min(length, b)) for a, b in facecam_claude or [] if min(length, b) - max(0.0, a) >= 2.0]
        if not wins:
            top = max((m for m in marks_out if m.kind in ("susto", "grito", "rabia", "risa", "queja", "sorpresa")
                       and m.strength >= 0.8), key=lambda m: m.strength, default=None)
            if top and length >= 8:
                a = max(0.0, top.t - 0.3)
                b = min(length, a + 3.0 + 3.0 * (top.strength - 0.8) / 0.2)
                if b - a >= 2.5:
                    wins = [(a, b)]
        plan.facecam = wins[:2]
        plan.facecam_px = geo.cam_px() if wins else None
        busy += plan.facecam

    # 2) Zoom al texto que provoca la reacción (va antes que la reacción).
    for m in sorted((m for m in marks_out if m.kind == "texto"), key=lambda m: m.t):
        if m.zona == "chat":
            box = geo.from_source(zona_chat) if zona_chat else None
        else:
            box = geo.from_source(ZONAS.get(m.zona) or ZONAS["juego"])
        if not box:
            continue
        a = max(0.0, m.t - 1.2)
        b = min(length, a + 2.0)
        if b - a < 1.2 or _overlaps(a, b, busy, 0.3):
            continue
        f, zx, zy = zoom_to_box(box)
        plan.zooms.append(Zoom(a, b, f, zx, zy, "texto", 0.2, 0.25))
        busy.append((a, b))

    # 3) Punch-ins a la cara en las emociones (uno cada ~4 s como mínimo; ninguno en pausas).
    if bloque not in SIN_PUNCH:
        punches: list[float] = []
        for m in sorted((m for m in marks_out if m.kind != "texto"), key=lambda m: -m.strength):
            if not has_cam:
                # Sin recuadro de cámara no hay cara que ampliar: solo un zoom suave en el remate más fuerte.
                if not punches and m.strength >= 0.6:
                    a, b = max(0.0, m.t - 0.35), min(length, m.t + 2.2)
                    if b - a >= 1.0 and not _overlaps(a, b, busy, 0.3):
                        plan.zooms.append(Zoom(a, b, 1.12, 0.5, 0.5, "punch", 0.35, 0.35))
                        busy.append((a, b))
                        punches.append(m.t)
                continue
            a = max(0.0, m.t - 0.12)
            b = min(length, a + 2.0 + 1.6 * m.strength)
            if b - a < 1.0 or _overlaps(a, b, busy, 0.4) or any(abs(m.t - p) < 4.0 for p in punches):
                continue
            plan.zooms.append(Zoom(a, b, round(1.28 + 0.22 * m.strength, 3), ax, ay, "punch"))
            busy.append((a, b))
            punches.append(m.t)

    # 4) Remate de monólogo: zoom continuo hasta 2x en los últimos ~4 s.
    if zoom_final and has_cam and length > 6:
        a = length - 4.0
        plan.zooms = [z for z in plan.zooms if z.t1 <= a - 0.2]
        plan.facecam = [(x, y) for x, y in plan.facecam if y <= a - 0.2]
        plan.zooms.append(Zoom(a, length + 1.0, 2.0, ax, ay, "final", 0.0, 0.0))

    plan.zooms.sort(key=lambda z: z.t0)
    if captions_words is not None and bloque not in SIN_CAPTIONS:
        plan.captions = key_captions(marks_out, captions_words, length)
    elif captions_words is not None:
        plan.captions = []
    return plan


def direct(cfg: dict, db, session: dict, spec, cand: dict | None, item: dict | None, out_size: tuple[int, int], *,
           captions: bool = False, allow_facecam: bool = True) -> Plan | None:
    """Arma el plan de efectos de un tramo (ClipSpec) y lo deja en spec.plan."""
    from .editor import _src_size

    if not cfg["edicion"].get("zoom", True) and not captions:
        return None
    streamer = get_streamer(cfg, spec.slug) or {"modo": "juego_cara", "camara": None}
    try:
        geo = Geometry(_src_size(spec.path), out_size, streamer.get("modo", "juego_cara"), streamer.get("camara"))
        env = loudness(spec.path, spec.file_start, spec.dur)
    except Exception as exc:  # noqa: BLE001 - sin plan el clip se arma igual
        log.warning("[%s] sin plan de efectos: %s", spec.slug, exc)
        return None
    item = item or {}
    marks = shout_marks(env) + lexicon_marks(spec.words, env) + \
        cue_marks(spec.words, bool(streamer.get("zona_chat")))
    if streamer.get("zona_chat"):
        rows = db.query("SELECT ts, usuario, texto FROM chat_messages WHERE session_id=? AND slug=? AND ts BETWEEN ? AND ?",
                        (session["id"], spec.slug, spec.wall0 - 5, spec.wall0 + spec.dur))
        marks += chat_read_marks(spec.words, [(r["ts"] - spec.wall0, r["usuario"] or "", r["texto"] or "") for r in rows])
    if cand:
        marks += claude_marks(item, cand["start_ts"] - spec.wall0)
    elif spec.momento is not None:
        marks.append(Mark(spec.momento, "remate", 0.75, "claude"))
    marks = merge_marks([m for m in marks if 0 <= m.t <= spec.dur])
    keep = spec.keep or [(0.0, spec.dur)]
    out_marks = []
    for m in marks:
        t = effects.remap(m.t, keep)
        if t is None:  # cayó en un silencio recortado: se usa el comienzo del tramo siguiente si está cerca
            nxt = next((a for a, _b in keep if 0 <= a - m.t <= 0.6), None)
            t = effects.remap(nxt, keep) if nxt is not None else None
        if t is not None:
            out_marks.append(Mark(t, m.kind, m.strength, m.source, m.zona, m.text))
    length = spec.kept_duration
    fc = []
    if cand:
        off = cand["start_ts"] - spec.wall0
        for w in item.get("facecam_completo") or []:
            a, b = effects.remap(float(w.get("inicio", 0)) + off, keep), effects.remap(float(w.get("fin", 0)) + off, keep)
            if a is not None and b is not None and b > a:
                fc.append((a, b))
    plan = build_plan(out_marks, length, geo, bloque=spec.bloque, zona_chat=streamer.get("zona_chat"),
                      captions_words=effects.remap_words(spec.words, keep) if captions else None,
                      zoom_final=bool(item.get("zoom_final")), facecam_claude=fc, allow_facecam=allow_facecam,
                      color=streamer_color(cfg, spec.slug))
    spec.plan = plan
    return plan


# ---------------------------------------------------------------------------
# Captions (ASS)
# ---------------------------------------------------------------------------
def ass_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&"


def build_key_ass(captions: list[tuple[float, float, str]], size: tuple[int, int], family: str, color: str) -> str:
    """Captions abajo al centro, fuente redondeada con contorno oscuro y el color del streamer."""
    w, h = size
    fs = int(min(w, h) * 0.085)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Key,{family},{fs},{ass_color(color)},&H000000FF,&H00141414,&H96000000,0,0,0,0,"
        f"100,100,1,0,1,{max(4, fs // 9)},{max(2, fs // 24)},2,{int(w * 0.05)},{int(w * 0.05)},{int(h * 0.075)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [f"Dialogue: 0,{effects._ass_time(a)},{effects._ass_time(b)},Key,,0,0,0,,"
             f"{{\\fscx128\\fscy128\\t(0,120,\\fscx100\\fscy100)}}{effects._ass_text(t)}" for a, b, t in captions]
    return header + "\n".join(lines) + "\n"


def write_key_captions(plan: Plan, size: tuple[int, int], work: Path, name: str) -> tuple[str, str] | None:
    import shutil

    from PIL import ImageFont

    if not plan.captions:
        return None
    font = CAPTION_FONT if CAPTION_FONT.exists() else None
    if not font:
        log.warning("No está la fuente de captions %s; se omiten", CAPTION_FONT)
        return None
    family = ImageFont.truetype(str(font), 20).getname()[0]
    fonts = work / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    if not (fonts / font.name).exists():
        shutil.copy2(font, fonts / font.name)
    ass = work / f"{name}.ass"
    ass.write_text(build_key_ass(plan.captions, size, family, plan.color), encoding="utf-8")
    return ass.name, "fonts"
