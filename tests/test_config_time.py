from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from clipmax.config import DEFAULTS, ConfigError, deep_merge, slug_from_url, validate
from clipmax.timeutil import current_window, fmt_duration, session_window


def test_slug_from_url():
    assert slug_from_url("https://kick.com/Westcol") == "westcol"
    assert slug_from_url("kick.com/gear_of-nos?x=1") == "gear_of-nos"
    assert slug_from_url("westcol") == "westcol"
    with pytest.raises(ConfigError):
        slug_from_url("https://twitch.tv/")


def test_validate_normalizes_streamers(cfg):
    west = cfg["streamers"][0]
    assert west["slug"] == "westcol"
    assert "Westcol" in west["alias"] and "westcol" in west["alias"]
    gear = cfg["streamers"][1]
    assert gear["modo"] == "cara" and gear["camara"]["w"] == pytest.approx(0.3)
    assert cfg["evento"]["pareja_principal"] == ["westcol", "gearofnos"]


def test_validate_rejects_bad_values():
    with pytest.raises(ConfigError):
        validate(deep_merge(DEFAULTS, {"evento": {"hora_inicio": "3pm"}}))
    with pytest.raises(ConfigError):
        validate(deep_merge(DEFAULTS, {"streamers": [{"url": "https://kick.com/a", "modo": "otro"}]}))
    with pytest.raises(ConfigError):
        validate(deep_merge(DEFAULTS, {"streamers": [{"url": "https://kick.com/a"}, {"url": "kick.com/a"}]}))


def test_vertical_format_swaps_resolution():
    cfg = validate(deep_merge(DEFAULTS, {"edicion": {"formato": "vertical", "ancho": 1920, "alto": 1080}}))
    assert (cfg["edicion"]["ancho"], cfg["edicion"]["alto"]) == (1080, 1920)


def test_session_window_bogota(cfg):
    start, end = session_window(cfg, date(2026, 9, 23))
    local = datetime.fromtimestamp(start, ZoneInfo("America/Bogota"))
    assert (local.hour, local.minute) == (15, 0)
    assert end - start == pytest.approx(8 * 3600 + 15 * 60)
    # 15:00 en Bogotá = 20:00 UTC
    assert datetime.fromtimestamp(start, ZoneInfo("UTC")).hour == 20


def test_current_window_crosses_midnight(cfg):
    cfg["evento"]["hora_inicio"] = "20:00"
    start, _ = session_window(cfg, date(2026, 9, 23))
    after_midnight = start + 5 * 3600  # 01:00 del día siguiente
    win = current_window(cfg, after_midnight)
    assert win and win[0] == "2026-09-23"
    assert current_window(cfg, start - 60) is None


def test_fmt_duration():
    assert fmt_duration(65) == "1:05"
    assert fmt_duration(3725) == "1h 02m 05s"
