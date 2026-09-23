"""Narración con voz, 100 % local y gratis (opcional).

- "tarjetas": sin voz; la narración se muestra como texto en pantalla.
- "sapi":     voces de Windows (Configuración > Hora e idioma > Voz; instala
              una voz en español, p. ej. "Microsoft Sabina" o "Helena").
- "piper":    TTS neuronal local (github.com/rhasspy/piper), suena más natural.
Si la voz falla por cualquier motivo, se vuelve a "tarjetas" sin romper el render.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import tools
from .config import resolve_path

log = logging.getLogger(__name__)


def _clean(text: str) -> str:
    # Quita emojis y símbolos que las voces leen raro.
    text = re.sub(r"[^\w\s.,;:¡!¿?\-'\"()%$#áéíóúüñÁÉÍÓÚÜÑ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def synthesize(cfg: dict, text: str, out_wav: Path) -> bool:
    engine = cfg["edicion"]["narracion"]
    text = _clean(text)
    if engine == "tarjetas" or not text:
        return False
    try:
        if engine == "sapi":
            return _sapi(cfg, text, out_wav)
        if engine == "piper":
            return _piper(cfg, text, out_wav)
    except Exception as exc:  # noqa: BLE001
        log.warning("TTS (%s) falló, uso solo texto: %s", engine, exc)
    return False


def _sapi(cfg: dict, text: str, out_wav: Path) -> bool:
    import pyttsx3  # solo Windows (requirements lo instala allí)

    try:  # SAPI es COM: cada hilo (el pipeline corre en uno aparte) debe inicializarlo.
        import comtypes

        comtypes.CoInitialize()
    except Exception:  # noqa: BLE001
        pass
    engine = pyttsx3.init()
    wanted = (cfg["edicion"].get("voz_sapi") or "").lower()
    voices = engine.getProperty("voices")
    chosen = None
    for v in voices:
        name = f"{v.name} {v.id}".lower()
        if wanted and wanted in name:
            chosen = v
            break
        if not wanted and any(k in name for k in ("spanish", "español", "es-", "sabina", "helena", "laura", "pablo")):
            chosen = chosen or v
    if chosen:
        engine.setProperty("voice", chosen.id)
    else:
        log.warning("No encontré una voz SAPI en español; se usará la voz por defecto")
    engine.setProperty("rate", 185)
    engine.save_to_file(text, str(out_wav))
    engine.runAndWait()
    return out_wav.exists() and out_wav.stat().st_size > 1000


def _piper(cfg: dict, text: str, out_wav: Path) -> bool:
    exe = resolve_path(cfg["edicion"]["piper_exe"])
    voice = resolve_path(cfg["edicion"]["piper_voz"])
    if not exe.exists() or not voice.exists():
        log.warning("Piper no configurado (%s / %s)", exe, voice)
        return False
    tools.run([str(exe), "--model", str(voice), "--output_file", str(out_wav)],
              timeout=120, input_bytes=text.encode("utf-8"))
    return out_wav.exists() and out_wav.stat().st_size > 1000
