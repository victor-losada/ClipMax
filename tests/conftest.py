import copy
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipmax.config import DEFAULTS, ConfigStore, deep_merge, validate  # noqa: E402
from clipmax.db import Database  # noqa: E402

STREAMERS = [
    {"nombre": "Westcol", "url": "https://kick.com/westcol", "alias": ["west", "wescol"],
     "transcribir_en_vivo": True, "prioridad": 1.5},
    {"nombre": "Gear of Nos", "url": "https://kick.com/gearofnos", "alias": ["gear", "gear of nos"],
     "transcribir_en_vivo": True, "prioridad": 1.5, "modo": "cara",
     "camara": {"x": 0.7, "y": 0.0, "w": 0.3, "h": 0.3}},
    {"nombre": "Otro", "url": "https://kick.com/otro", "alias": []},
]


@pytest.fixture
def cfg(tmp_path):
    raw = {"streamers": copy.deepcopy(STREAMERS),
           "grabacion": {"carpeta_datos": str(tmp_path / "data")},
           "edicion": {"ancho": 640, "alto": 360, "preset": "ultrafast", "duracion_min_min": 0.1,
                       "duracion_max_min": 5}}
    return validate(deep_merge(DEFAULTS, raw))


@pytest.fixture
def store(tmp_path, cfg):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return ConfigStore(path)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
