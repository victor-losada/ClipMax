"""El lanzador corrige la carpeta 'ClipMax' -> 'clipmax' (error 'No module named clipmax' en Windows)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("arrancar", ROOT / "arrancar.py")
arrancar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arrancar)


def _make_pkg(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "__init__.py").write_text("")
    (path / "__main__.py").write_text("")


def test_renames_wrong_case_folder(tmp_path):
    _make_pkg(tmp_path / "ClipMax")
    result = arrancar.ensure_package(tmp_path)
    assert result == tmp_path / "clipmax"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clipmax"]


def test_correct_folder_untouched(tmp_path):
    _make_pkg(tmp_path / "clipmax")
    assert arrancar.ensure_package(tmp_path) == tmp_path / "clipmax"


def test_reports_nested_copy(tmp_path):
    _make_pkg(tmp_path / "ClipMax-main" / "clipmax")
    (tmp_path / ".venv" / "x" / "clipmax").mkdir(parents=True)  # la .venv no se revisa
    with pytest.raises(SystemExit) as exc:
        arrancar.ensure_package(tmp_path)
    msg = str(exc.value)
    assert "No encuentro la carpeta" in msg and str(tmp_path / "ClipMax-main" / "clipmax") in msg


def test_reports_missing_package(tmp_path):
    (tmp_path / "docs").mkdir()
    with pytest.raises(SystemExit) as exc:
        arrancar.ensure_package(tmp_path)
    assert "Download ZIP" in str(exc.value)
