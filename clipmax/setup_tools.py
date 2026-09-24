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
import ssl
import sys
import urllib.error
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


class DownloadError(RuntimeError):
    def __init__(self, url: str, dest: Path, reason: str):
        super().__init__(reason)
        self.url, self.dest, self.reason = url, dest, reason


def _is_cert_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _download(url: str, dest: Path) -> Path:
    """Descarga con barra de progreso. Si falla el certificado, reintenta con las raíces de certifi."""
    from .netssl import certifi_context

    try:
        return _fetch(url, dest, None)
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        if _is_cert_error(exc):
            print("  Certificado no verificado con el almacén de Windows; reintento con certifi…")
            try:
                return _fetch(url, dest, certifi_context())
            except (urllib.error.URLError, ssl.SSLError, OSError) as exc2:
                exc = exc2
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
        raise DownloadError(url, dest, str(getattr(exc, "reason", exc))) from exc


def _fetch(url: str, dest: Path, context) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"Descargando {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ClipMax/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=context) as resp, open(tmp, "wb") as fh:
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


def install_whisper(cuda: bool = False, force: bool = False) -> None:
    target = BIN / "whisper"
    existing = list(target.glob("**/whisper-cli.exe")) or list(target.glob("**/whisper-cli"))
    if existing and not force:
        print(f"Ya existe whisper-cli: {existing[0]} (usa --forzar para reinstalar)")
        return
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


def run(models: list[str], ffmpeg: bool, cuda: bool, vad: bool, skip_whisper: bool,
        force: bool = False) -> bool:
    """Descarga todo lo pedido. Un fallo no detiene el resto; al final se resume. True = todo bien."""
    if sys.platform != "win32" and not skip_whisper:
        print("Aviso: los binarios que se descargan son para Windows x64. En Linux/macOS compila whisper.cpp.")
    jobs = []
    if not skip_whisper:
        jobs.append(lambda: install_whisper(cuda, force))
    jobs += [(lambda m=m: install_model(m)) for m in models]
    if vad:
        jobs.append(install_vad)
    if ffmpeg:
        jobs.append(install_ffmpeg)
    failed: list[DownloadError] = []
    for job in jobs:
        try:
            job()
        except DownloadError as exc:
            print(f"  ERROR: {exc.reason}")
            failed.append(exc)
    if not failed:
        print("Listo. Ejecuta `python arrancar.py doctor` para verificar.")
        return True
    print("\n" + "=" * 70)
    print(f" {len(failed)} descarga(s) fallaron. Puedes bajarlas con el navegador y guardarlas así:")
    for exc in failed:
        print(f"   {exc.url}\n     -> guardar como: {exc.dest}")
    print(" Luego ejecuta de nuevo instalar.bat (lo que ya está descargado no se repite).")
    print("=" * 70)
    return False
