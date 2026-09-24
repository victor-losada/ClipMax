"""Lanzador de ClipMax (lo usan instalar.bat e iniciar.bat).

`python arrancar.py <comando>` equivale a `python -m clipmax <comando>`, pero
antes verifica que la carpeta del código esté bien:

- Windows no distingue mayúsculas en los nombres de carpeta, pero Python sí al
  importar: si la carpeta quedó como "ClipMax" o "CLIPMAX" (típico al copiar o
  descomprimir a mano), `python -m clipmax` falla con "No module named clipmax".
  Aquí se renombra sola a "clipmax".
- Si la carpeta no está donde debe, explica qué falta y dónde la encontró.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = "clipmax"
_SKIP = {".venv", "venv", "data", "data_demo", "bin", "models", ".git", "__pycache__", "node_modules"}


def _is_package(path: Path) -> bool:
    return path.is_dir() and (path / "__main__.py").is_file() and (path / "__init__.py").is_file()


def _search_elsewhere(root: Path, max_depth: int = 3) -> list[Path]:
    """Busca la carpeta del paquete en subcarpetas (p. ej. un ZIP descomprimido dentro de otro)."""
    found = []
    base_depth = len(root.parts)
    for dirpath, dirnames, _files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP]
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
        for d in dirnames:
            p = Path(dirpath) / d
            if d.lower() == PKG and Path(dirpath) != root and _is_package(p):
                found.append(p)
    return found


def ensure_package(root: Path = ROOT) -> Path:
    """Devuelve la ruta del paquete; corrige mayúsculas o termina con instrucciones claras."""
    for name in os.listdir(root):
        p = root / name
        if name.lower() != PKG or not _is_package(p):
            continue
        if name != PKG:
            # Renombrado en dos pasos: en Windows un cambio solo de mayúsculas puede no aplicarse.
            tmp = root / f"{PKG}__renombrando"
            try:
                p.rename(tmp)
                tmp.rename(root / PKG)
            except OSError as exc:
                raise SystemExit(
                    f"\n[ClipMax] La carpeta del código se llama '{name}' y debe llamarse '{PKG}' "
                    f"(en minúsculas), pero no pude renombrarla ({exc}).\n"
                    "Cierra VS Code o el Explorador que la tenga abierta y ejecuta en esta consola:\n"
                    f'  cd /d "{root}"\n  ren "{name}" {PKG}_tmp\n  ren {PKG}_tmp {PKG}\n'
                ) from exc
            print(f"[ClipMax] Renombré la carpeta '{name}' a '{PKG}' (Python distingue mayúsculas).")
        return root / PKG

    msg = [f"\n[ClipMax] No encuentro la carpeta del código '{PKG}' dentro de {root}",
           f"Debe existir este archivo: {root / PKG / '__main__.py'}"]
    elsewhere = _search_elsewhere(root)
    if elsewhere:
        msg.append("La encontré en otro lugar:")
        msg += [f"  {p}" for p in elsewhere]
        msg.append(f"Mueve esa carpeta (con todo su contenido) a {root} para que quede como {root / PKG}")
        msg.append("o ejecuta los .bat que están junto a ella.")
    else:
        msg.append("Vuelve a descargar el proyecto completo (en GitHub: Code -> Download ZIP) y copia "
                   f"la carpeta '{PKG}' entera junto a este archivo.")
    raise SystemExit("\n".join(msg) + "\n")


def main() -> None:
    ensure_package()
    sys.path.insert(0, str(ROOT))
    runpy.run_module(PKG, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
