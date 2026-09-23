"""Descarga de herramientas locales para Windows: whisper.cpp, modelos y (opcional) ffmpeg.

Todo va a bin/ y models/ dentro del proyecto; no toca el sistema.
Si una URL cambia, el comando lo dice y puedes descargar el archivo a mano
(ver docs/INSTALACION_WINDOWS.md).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

BIN = PROJECT_ROOT / "bin"
MODELS = PROJECT_ROOT / "models"

RELEASES_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=30"
# Si la API de GitHub no responde, se usa esta versión conocida con binarios para Windows.
WHISPER_FALLBACK = "https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.6/{asset}"
FFMPEG_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
VAD_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin"


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"Descargando {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ClipMax/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                sys.stdout.write(f"\r  {done / total:6.1%} de {total / 1e6:.0f} MB")
                sys.stdout.flush()
    print()
    tmp.replace(dest)
    return dest


def _unzip(zip_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)
    zip_path.unlink(missing_ok=True)


def _whisper_asset_url(cuda: bool) -> str:
    """Busca la release más reciente que publique binarios de Windows (no todas los traen)."""
    wanted = re.compile(r"whisper-cublas-12[\d.]*-bin-x64\.zip" if cuda else r"whisper-bin-x64\.zip")
    try:
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": "ClipMax/1.0",
                                                             "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
        for rel in releases:
            if rel.get("prerelease") or rel.get("draft"):
                continue
            for asset in rel.get("assets", []):
                if wanted.fullmatch(asset["name"]):
                    print(f"whisper.cpp {rel['tag_name']}: {asset['name']}")
                    return asset["browser_download_url"]
    except Exception as exc:  # noqa: BLE001
        print(f"No pude consultar las releases de GitHub ({exc}); uso v1.8.6")
    return WHISPER_FALLBACK.format(asset="whisper-cublas-12.4.0-bin-x64.zip" if cuda else "whisper-bin-x64.zip")


def install_whisper(cuda: bool = False) -> None:
    target = BIN / "whisper"
    if target.exists():
        shutil.rmtree(target)
    z = _download(_whisper_asset_url(cuda), BIN / "whisper.zip")
    _unzip(z, target)
    found = list(target.glob("**/whisper-cli.exe")) or list(target.glob("**/whisper-cli"))
    print("whisper-cli:", found[0] if found else "NO ENCONTRADO (revisa bin/whisper)")


def install_model(name: str) -> None:
    dest = MODELS / f"ggml-{name}.bin"
    if dest.exists():
        print(f"Ya existe {dest.name}")
        return
    _download(MODEL_URL.format(name=name), dest)


def install_vad() -> None:
    dest = MODELS / "ggml-silero-v5.1.2.bin"
    if not dest.exists():
        _download(VAD_URL, dest)


def install_ffmpeg() -> None:
    target = BIN / "ffmpeg"
    if target.exists():
        shutil.rmtree(target)
    z = _download(FFMPEG_ZIP, BIN / "ffmpeg.zip")
    _unzip(z, target)
    found = list(target.glob("**/ffmpeg.exe"))
    print("ffmpeg:", found[0] if found else "NO ENCONTRADO (revisa bin/ffmpeg)")


def run(models: list[str], ffmpeg: bool, cuda: bool, vad: bool, skip_whisper: bool) -> None:
    if sys.platform != "win32" and not skip_whisper:
        print("Aviso: los binarios que se descargan son para Windows x64. En Linux/macOS compila whisper.cpp.")
    if not skip_whisper:
        install_whisper(cuda)
    for m in models:
        install_model(m)
    if vad:
        install_vad()
    if ffmpeg:
        install_ffmpeg()
    print("Listo. Ejecuta `python -m clipmax doctor` para verificar.")
