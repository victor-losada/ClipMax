"""Localización y ejecución de binarios externos: ffmpeg, ffprobe, yt-dlp, whisper.cpp.

Orden de búsqueda de cada binario:
  1. Ruta explícita en config.yaml (si se definió).
  2. Carpeta bin/ del proyecto (lo que deja `python -m clipmax descargar`).
  3. El PATH del sistema (por ejemplo, ffmpeg instalado con winget).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT, resolve_path
from .winutil import IS_WINDOWS, POPEN_FLAGS

log = logging.getLogger(__name__)

BIN_DIR = PROJECT_ROOT / "bin"


class ToolMissing(RuntimeError):
    pass


def _exe(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS and not name.endswith(".exe") else name


def find_binary(name: str, configured: str = "", extra_dirs: tuple[str, ...] = ()) -> str | None:
    if configured:
        p = resolve_path(configured)
        if p.exists():
            return str(p)
        log.warning("La ruta configurada para %s no existe: %s", name, p)
    candidates = [BIN_DIR / _exe(name)]
    candidates += [BIN_DIR / d / _exe(name) for d in extra_dirs]
    # Los zips oficiales suelen traer subcarpetas (ffmpeg-x/bin, whisper/Release...).
    candidates += sorted(BIN_DIR.glob(f"**/{_exe(name)}"))
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which(name)


def ffmpeg(cfg: dict | None = None) -> str:
    path = find_binary("ffmpeg")
    if not path:
        raise ToolMissing(
            "No encuentro ffmpeg. Instálalo con `winget install Gyan.FFmpeg` "
            "o ejecuta `python -m clipmax descargar --ffmpeg`."
        )
    return path


def ffprobe(cfg: dict | None = None) -> str | None:
    return find_binary("ffprobe")


def whisper_cli(cfg: dict) -> str:
    configured = cfg["transcripcion"].get("whisper_cli", "")
    path = find_binary("whisper-cli", configured) or find_binary("main", "", ("whisper",))
    if not path:
        raise ToolMissing(
            "No encuentro whisper-cli. Ejecuta `python -m clipmax descargar` o descarga "
            "whisper-bin-x64.zip desde github.com/ggml-org/whisper.cpp/releases y descomprímelo en bin/whisper."
        )
    return path


def ytdlp_cmd() -> list[str]:
    """Preferimos el yt-dlp instalado con pip en el mismo entorno (se actualiza con pip)."""
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        path = find_binary("yt-dlp")
        if not path:
            raise ToolMissing("No encuentro yt-dlp. Ejecuta `pip install -r requirements.txt`.")
        return [path]


def run(cmd: list[str], timeout: float | None = None, check: bool = True,
        capture: bool = True, input_bytes: bytes | None = None,
        cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """subprocess.run sin ventana de consola en Windows y con log de errores legible."""
    log.debug("Ejecutando: %s", " ".join(map(str, cmd)))
    proc = subprocess.run(
        [str(c) for c in cmd],
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
        creationflags=POPEN_FLAGS,
        cwd=str(cwd) if cwd else None,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"Falló {Path(str(cmd[0])).name} (código {proc.returncode}):\n{err}")
    return proc


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path: str | Path) -> float:
    """Duración en segundos. Usa ffprobe si existe; si no, parsea la salida de `ffmpeg -i`."""
    probe = ffprobe()
    if probe:
        proc = run([probe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(path)], timeout=120, check=False)
        try:
            return float(json.loads(proc.stdout or b"{}")["format"]["duration"])
        except (KeyError, ValueError, TypeError):
            pass
    proc = run([ffmpeg(), "-hide_banner", "-i", str(path)], timeout=120, check=False)
    match = _DUR_RE.search((proc.stderr or b"").decode("utf-8", "replace"))
    if not match:
        # Archivos .ts que se están escribiendo no siempre reportan duración: los decodificamos.
        proc = run([ffmpeg(), "-hide_banner", "-i", str(path), "-map", "0:a:0?", "-map", "0:v:0?",
                    "-c", "copy", "-f", "null", "-"], timeout=600, check=False)
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", (proc.stderr or b"").decode("utf-8", "replace"))
        if not times:
            return 0.0
        h, m, s = times[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


_SIZE_RE = re.compile(r"Stream #\S+.*Video:.*?(\d{2,5})x(\d{2,5})")


def probe_video_size(path: str | Path) -> tuple[int, int]:
    """(ancho, alto) del primer stream de video. (1920, 1080) si no se puede leer."""
    probe = ffprobe()
    if probe:
        proc = run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=width,height", "-of", "json", str(path)], timeout=60, check=False)
        try:
            st = json.loads(proc.stdout or b"{}")["streams"][0]
            return int(st["width"]), int(st["height"])
        except (KeyError, IndexError, ValueError, TypeError):
            pass
    proc = run([ffmpeg(), "-hide_banner", "-i", str(path)], timeout=60, check=False)
    match = _SIZE_RE.search((proc.stderr or b"").decode("utf-8", "replace"))
    return (int(match.group(1)), int(match.group(2))) if match else (1920, 1080)


def version_of(cmd: list[str]) -> str:
    try:
        proc = run(cmd, timeout=30, check=False)
        text = (proc.stdout or b"").decode("utf-8", "replace") or (proc.stderr or b"").decode("utf-8", "replace")
        return text.strip().splitlines()[0] if text.strip() else "?"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def concat_list_line(path: str | Path) -> str:
    """Línea para el demuxer concat de ffmpeg (rutas de Windows con / y comillas escapadas)."""
    p = str(Path(path).resolve()).replace("\\", "/").replace("'", "'\\''")
    return f"file '{p}'\n"
