"""Carga, validación y guardado de la configuración (config.yaml).

La configuración es un dict anidado. Todo lo que el usuario no define se
completa con DEFAULTS mediante un merge profundo, así un config.yaml mínimo
funciona y los campos nuevos de versiones futuras no rompen archivos viejos.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Carpeta raíz del proyecto (donde están config.yaml, bin/, models/, prompts/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.yaml"

MODOS_GRABACION = ("juego_cara", "cara")
DIAS = ("lun", "mar", "mie", "jue", "vie", "sab", "dom")

DEFAULTS: dict[str, Any] = {
    "evento": {
        "nombre": "Desafío 4",
        "zona_horaria": "America/Bogota",
        "hora_inicio": "15:00",
        "duracion_horas": 8,
        "margen_final_min": 15,
        "dias_activos": list(DIAS),
        "programacion_activa": True,
        "procesar_al_terminar": True,
        # Slugs cuyo chipeo se prioriza por encima de todo lo demás.
        "pareja_principal": ["westcol", "gearofnos"],
        # Notas fijas de lore que Claude recibe cada día (rivalidades, alianzas...).
        "lore_base": "",
    },
    "streamers": [],
    "grabacion": {
        "carpeta_datos": "data",
        "calidad_max": 720,          # altura máxima (p) de la variante a grabar
        "resolver": "yt-dlp",        # "yt-dlp" o "api" (lee playback_url de Kick)
        "reintento_s": 45,           # espera entre chequeos si el streamer no está en vivo
        "estancado_s": 90,           # si el archivo no crece en este tiempo, reinicia ffmpeg
        "borrar_partes_ts": True,    # borra las partes .ts después de crear el MP4 final
        "desfase_chat_s": 0.0,       # compensación manual entre reloj del chat y del video
    },
    "chat": {
        "guardar_mensajes": True,
        "pusher_keys": ["32cbd69e4b950bf97679", "eb1d5f283081a78b932c"],
        "pusher_cluster": "us2",
        "palabras_hype": [
            "kekw", "omegalul", "lul", "lmao", "jaja", "jsjs", "xd", "clip", "clipeo",
            "noo", "nooo", "que", "wtf", "f", "w", "l", "humillado", "humillo", "tilin",
            "gg", "pog", "pogchamp", "😂", "🤣", "💀", "🔥", "?????",
        ],
    },
    "deteccion": {
        "bucket_s": 5,
        "suavizado_buckets": 3,
        "ventana_base_min": 10,
        "umbral_z": 3.5,
        "ratio_min": 1.8,
        "mensajes_min_bucket": 4,
        "separacion_picos_s": 60,
        "sincronia_s": 45,
        "umbral_menciones_chat": 3,   # menciones del otro streamer por bucket para contar como señal
        "pre_s": 50,                  # segundos antes de la señal que se incluyen en el candidato
        "post_s": 25,
        "duracion_max_candidato_s": 180,
        "candidatos_max": 40,
        "pesos": {
            "pico_chat": 1.0,
            "mencion_voz": 2.5,
            "mencion_chat": 0.8,
            "sincronia": 1.2,
            "tema_x": 1.0,
        },
        "multiplicador_pareja": 1.6,
    },
    "transcripcion": {
        "whisper_cli": "",                      # vacío = buscar en bin/ y en el PATH
        "modelo_vivo": "models/ggml-base.bin",  # rápido, para escuchar menciones en vivo
        "modelo_calidad": "models/ggml-small.bin",  # para los candidatos que van a Claude
        "vad_modelo": "",                       # opcional: models/ggml-silero-v5.1.2.bin
        "idioma": "es",
        "hilos": 4,
        "intervalo_vivo_s": 60,
        "margen_vivo_s": 20,
        "prompt_inicial": "Desafío 4, Minecraft, Kick, Westcol, Gear of Nos, chat, stream.",
    },
    "x": {
        "modo": "manual",   # "manual" | "api" | "claude_web"
        "consulta": '("Desafío 4" OR "desafio 4" OR westcol OR gearofnos) -is:retweet',
        "intervalo_min": 60,
        "max_consultas_dia": 8,
        "max_caracteres_contexto": 12000,
        "busquedas_web_max": 5,
        # Si es true, el post-proceso se detiene antes de llamar a Claude hasta que pegues el
        # contexto de X del día (p. ej. la salida de Grok); al pegarlo, continúa solo.
        "esperar_contexto": False,
        # Cuentas oficiales del evento (sin @) que el prompt para Grok manda revisar primero.
        "cuentas": [],
    },
    "claude": {
        "modo": "api",                  # "api" o "manual" (pegar en claude.ai)
        "modelo": "claude-opus-5",
        "esfuerzo": "high",             # low | medium | high | xhigh | max
        "max_tokens": 32000,
        "presupuesto_mensual_usd": 10.0,
        "fallback_por_rechazo": True,   # reintento automático en otro modelo si hay rechazo
        "max_caracteres_transcripcion": 2500,  # por candidato
        "precios": {                    # USD por millón de tokens (entrada, salida)
            "claude-opus-5": [5.0, 25.0],
            "claude-opus-5-5": [4.0, 20.0],
            "claude-sonnet-5": [2.0, 10.0],
            "claude-haiku-4-5": [1.0, 5.0],
            "claude-opus-4-8": [5.0, 25.0],
        },
        "precio_busqueda_web_usd": 0.01,
    },
    "edicion": {
        "formato": "horizontal",   # "horizontal" (1920x1080) o "vertical" (1080x1920)
        "ancho": 1920,
        "alto": 1080,
        "fps": 30,
        "codec": "libx264",        # "h264_nvenc" si tienes GPU NVIDIA
        "crf": 21,
        "preset": "veryfast",
        "duracion_min_min": 10,
        "duracion_max_min": 20,
        "silencio_max_s": 2.5,
        "metodo_silencios": "transcripcion",   # "transcripcion" | "audio" | "ninguno"
        "umbral_silencio_db": -38,
        "narracion": "tarjetas",   # "tarjetas" | "sapi" | "piper"
        "piper_exe": "bin/piper/piper.exe",
        "piper_voz": "models/es_MX-claude-high.onnx",
        "voz_sapi": "",            # parte del nombre de la voz, ej. "Sabina"
        "fuente": "",              # vacío = Segoe UI Bold / Arial Bold en Windows
        "exportar_clips_tiktok": True,
        "titulos_en_pantalla": True,
        # Efectos (ver clipmax/effects.py)
        "subtitulos": True,            # subtítulos dinámicos: la palabra que se dice se ilumina
        "subtitulos_palabras": 3,      # palabras visibles a la vez
        "subtitulos_fuente": "",       # .ttf; vacío = Arial Black / Impact / Segoe UI Black
        "zoom": True,                  # zoom suave en el remate
        "zoom_factor": 1.12,
        "zoom_auto": True,             # si Claude no marca el remate, usar el pico de chat
        "pantalla_dividida": True,     # los dos streamers a la par cuando Claude lo pide
        "efectos_sonido": True,
        "sfx_volumen": 0.55,
        "sfx_transicion": "whoosh",    # al entrar a un clip después de una tarjeta ("" = ninguno)
        "sfx_max_por_video": 10,       # "uno que otro": tope de efectos en todo el resumen
        # Ajustes finos de sincronía (segundos; normalmente 0). + = más tarde, - = más temprano.
        "desfase_audio_s": 0.0,        # si en tus videos la voz llega antes/después que la imagen
        "subtitulos_desfase_s": 0.0,   # si los subtítulos salen antes/después de la voz
    },
    "web": {"host": "127.0.0.1", "puerto": 5000, "abrir_navegador": True},
}


class ConfigError(ValueError):
    """Error de validación con un mensaje legible para el usuario."""


def deep_merge(base: dict, override: dict) -> dict:
    """Devuelve una copia de ``base`` con ``override`` mezclado recursivamente."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_SLUG_RE = re.compile(r"kick\.com/([A-Za-z0-9_\-]+)", re.I)


def slug_from_url(url: str) -> str:
    """'https://kick.com/westcol' -> 'westcol'. Acepta también el slug directo."""
    url = (url or "").strip()
    match = _SLUG_RE.search(url)
    if match:
        return match.group(1).lower()
    if re.fullmatch(r"[A-Za-z0-9_\-]+", url):
        return url.lower()
    raise ConfigError(f"URL de Kick inválida: {url!r}")


def _normalize_streamer(raw: dict, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"streamers[{index}] debe ser un objeto")
    s = {
        "nombre": "",
        "url": "",
        "slug": "",
        "modo": "juego_cara",
        "activo": True,
        "prioridad": 1.0,
        "alias": [],
        "transcribir_en_vivo": False,
        "camara": None,
        "chatroom_id": None,
    }
    s.update({k: v for k, v in raw.items() if v is not None or k in ("camara", "chatroom_id")})
    if not s["url"] and not s["slug"]:
        raise ConfigError(f"streamers[{index}] necesita 'url'")
    s["slug"] = slug_from_url(s["slug"] or s["url"])
    if not s["url"]:
        s["url"] = f"https://kick.com/{s['slug']}"
    if not s["nombre"]:
        s["nombre"] = s["slug"]
    if s["modo"] not in MODOS_GRABACION:
        raise ConfigError(
            f"streamers[{index}].modo debe ser uno de {MODOS_GRABACION}, no {s['modo']!r}"
        )
    s["activo"] = bool(s["activo"])
    s["transcribir_en_vivo"] = bool(s["transcribir_en_vivo"])
    try:
        s["prioridad"] = float(s["prioridad"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"streamers[{index}].prioridad debe ser un número") from exc
    alias = s.get("alias") or []
    if isinstance(alias, str):
        alias = [a.strip() for a in alias.split(",")]
    # El nombre y el slug siempre cuentan como alias.
    base = [s["nombre"], s["slug"]]
    s["alias"] = sorted({a.strip() for a in [*alias, *base] if a and a.strip()}, key=str.lower)
    cam = s.get("camara")
    if cam:
        try:
            cam = {k: float(cam[k]) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                f"streamers[{index}].camara necesita x, y, w, h (fracciones 0-1)"
            ) from exc
        if not all(0 <= cam[k] <= 1 for k in cam) or cam["w"] <= 0 or cam["h"] <= 0:
            raise ConfigError(f"streamers[{index}].camara: valores fuera de rango 0-1")
        cam["w"] = min(cam["w"], 1 - cam["x"])
        cam["h"] = min(cam["h"], 1 - cam["y"])
        s["camara"] = cam
    else:
        s["camara"] = None
    if s.get("chatroom_id") not in (None, ""):
        s["chatroom_id"] = int(s["chatroom_id"])
    else:
        s["chatroom_id"] = None
    return s


def validate(cfg: dict) -> dict:
    """Valida y normaliza la configuración. Lanza ConfigError si algo está mal."""
    ev = cfg["evento"]
    if not re.fullmatch(r"\d{1,2}:\d{2}", str(ev["hora_inicio"])):
        raise ConfigError("evento.hora_inicio debe tener formato HH:MM (ej. 15:00)")
    h, m = (int(x) for x in str(ev["hora_inicio"]).split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ConfigError("evento.hora_inicio fuera de rango")
    ev["hora_inicio"] = f"{h:02d}:{m:02d}"
    ev["duracion_horas"] = float(ev["duracion_horas"])
    if not 0 < ev["duracion_horas"] <= 24:
        raise ConfigError("evento.duracion_horas debe estar entre 0 y 24")
    dias = [str(d).lower()[:3] for d in ev.get("dias_activos") or []]
    bad = [d for d in dias if d not in DIAS]
    if bad:
        raise ConfigError(f"evento.dias_activos tiene valores inválidos: {bad}. Usa {DIAS}")
    ev["dias_activos"] = dias

    streamers = [_normalize_streamer(s, i) for i, s in enumerate(cfg.get("streamers") or [])]
    slugs = [s["slug"] for s in streamers]
    dup = {x for x in slugs if slugs.count(x) > 1}
    if dup:
        raise ConfigError(f"Streamers duplicados: {sorted(dup)}")
    cfg["streamers"] = streamers
    ev["pareja_principal"] = [slug_from_url(p) for p in ev.get("pareja_principal") or []][:2]

    if cfg["claude"]["modo"] not in ("api", "manual"):
        raise ConfigError("claude.modo debe ser 'api' o 'manual'")
    if cfg["claude"]["esfuerzo"] not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("claude.esfuerzo debe ser low, medium, high, xhigh o max")
    cuentas = cfg["x"].get("cuentas") or []
    if isinstance(cuentas, str):
        cuentas = cuentas.split(",")
    cfg["x"]["cuentas"] = [str(c).strip().lstrip("@") for c in cuentas if str(c).strip()]
    cfg["x"]["esperar_contexto"] = bool(cfg["x"].get("esperar_contexto"))
    if cfg["x"]["modo"] not in ("manual", "api", "claude_web"):
        raise ConfigError("x.modo debe ser 'manual', 'api' o 'claude_web'")
    ed = cfg["edicion"]
    if ed["formato"] not in ("horizontal", "vertical"):
        raise ConfigError("edicion.formato debe ser 'horizontal' o 'vertical'")
    if ed["narracion"] not in ("tarjetas", "sapi", "piper"):
        raise ConfigError("edicion.narracion debe ser 'tarjetas', 'sapi' o 'piper'")
    if ed["metodo_silencios"] not in ("transcripcion", "audio", "ninguno"):
        raise ConfigError("edicion.metodo_silencios inválido")
    if float(ed["duracion_min_min"]) > float(ed["duracion_max_min"]):
        raise ConfigError("edicion.duracion_min_min no puede ser mayor que duracion_max_min")
    # Resolución coherente con el formato (pares, como exige yuv420p).
    for key in ("desfase_audio_s", "subtitulos_desfase_s"):
        try:
            ed[key] = round(float(ed.get(key) or 0.0), 3)
        except (TypeError, ValueError):
            raise ConfigError(f"edicion.{key} debe ser un número de segundos (ej. -0.3)") from None
        if abs(ed[key]) > 5:
            raise ConfigError(f"edicion.{key} debe estar entre -5 y 5 segundos")
    ed["ancho"] = int(ed["ancho"]) // 2 * 2
    ed["alto"] = int(ed["alto"]) // 2 * 2
    if ed["formato"] == "vertical" and ed["ancho"] > ed["alto"]:
        ed["ancho"], ed["alto"] = ed["alto"], ed["ancho"]
    if ed["formato"] == "horizontal" and ed["alto"] > ed["ancho"]:
        ed["ancho"], ed["alto"] = ed["alto"], ed["ancho"]
    return cfg


class ConfigStore:
    """Mantiene la configuración actual en memoria y la persiste en disco.

    Es thread-safe: la interfaz web la modifica mientras el scheduler la lee.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._cfg: dict = {}
        self.reload()

    def reload(self) -> dict:
        with self._lock:
            raw: dict = {}
            source = self.path
            if not source.exists() and EXAMPLE_CONFIG_PATH.exists():
                log.warning("No existe %s; usando %s", self.path, EXAMPLE_CONFIG_PATH.name)
                source = EXAMPLE_CONFIG_PATH
            if source.exists():
                with open(source, encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
            self._cfg = validate(deep_merge(DEFAULTS, raw))
            return self.get()

    def get(self) -> dict:
        """Copia profunda: quien la reciba puede modificarla sin efectos colaterales."""
        with self._lock:
            return copy.deepcopy(self._cfg)

    def save(self, new_cfg: dict) -> dict:
        """Valida y guarda. Los comentarios del YAML original se pierden (ver config.example.yaml)."""
        with self._lock:
            cfg = validate(deep_merge(DEFAULTS, new_cfg))
            # Guardamos los streamers sin el slug derivado duplicado cuando coincide con la URL.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("# Generado por ClipMax. Referencia comentada: config.example.yaml\n")
                yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False, width=100)
            tmp.replace(self.path)
            self._cfg = cfg
            return self.get()


# ---------------------------------------------------------------------------
# Ayudantes de consulta
# ---------------------------------------------------------------------------

def resolve_path(p: str | Path) -> Path:
    """Rutas relativas se interpretan desde la raíz del proyecto."""
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def data_dir(cfg: dict) -> Path:
    d = resolve_path(cfg["grabacion"]["carpeta_datos"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_dir(cfg: dict, fecha: str) -> Path:
    d = data_dir(cfg) / "sesiones" / fecha
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_streamers(cfg: dict) -> list[dict]:
    return [s for s in cfg["streamers"] if s["activo"]]


def get_streamer(cfg: dict, slug: str) -> dict | None:
    return next((s for s in cfg["streamers"] if s["slug"] == slug), None)


def pair_slugs(cfg: dict) -> list[str]:
    return list(cfg["evento"]["pareja_principal"])


def streamer_name(cfg: dict, slug: str) -> str:
    s = get_streamer(cfg, slug)
    return s["nombre"] if s else slug
