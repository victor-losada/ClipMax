"""Utilidades de tiempo. Internamente todo se guarda como epoch UTC (float).

La zona horaria del evento (por defecto America/Bogota, UTC-5 sin horario de
verano) solo se usa para decidir cuándo empieza/termina el día y para mostrar
horas legibles en la interfaz, el paquete para Claude y el reporte.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import DIAS


def tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["evento"]["zona_horaria"])


def now() -> float:
    return time.time()


def local_dt(ts: float, cfg: dict) -> datetime:
    return datetime.fromtimestamp(ts, tz(cfg))


def fmt_clock(ts: float, cfg: dict, seconds: bool = True) -> str:
    return local_dt(ts, cfg).strftime("%H:%M:%S" if seconds else "%H:%M")


def today_str(cfg: dict, ts: float | None = None) -> str:
    return local_dt(ts if ts is not None else now(), cfg).strftime("%Y-%m-%d")


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = str(value).split(":")
    return int(h), int(m)


def session_window(cfg: dict, day: date) -> tuple[float, float]:
    """(inicio, fin) en epoch para el día local ``day`` según la configuración."""
    h, m = parse_hhmm(cfg["evento"]["hora_inicio"])
    start = datetime(day.year, day.month, day.day, h, m, tzinfo=tz(cfg))
    dur = timedelta(hours=float(cfg["evento"]["duracion_horas"]),
                    minutes=float(cfg["evento"]["margen_final_min"]))
    return start.timestamp(), (start + dur).timestamp()


def day_is_active(cfg: dict, day: date) -> bool:
    return DIAS[day.weekday()] in cfg["evento"]["dias_activos"]


def current_window(cfg: dict, ts: float | None = None) -> tuple[str, float, float] | None:
    """Si ``ts`` cae dentro de una ventana de grabación, devuelve (fecha, inicio, fin).

    Revisa también la ventana del día anterior, porque una sesión que empieza
    tarde puede cruzar la medianoche.
    """
    ts = now() if ts is None else ts
    today = local_dt(ts, cfg).date()
    for day in (today, today - timedelta(days=1)):
        if not day_is_active(cfg, day):
            continue
        start, end = session_window(cfg, day)
        if start <= ts < end:
            return day.isoformat(), start, end
    return None


def next_window_start(cfg: dict, ts: float | None = None) -> float | None:
    ts = now() if ts is None else ts
    today = local_dt(ts, cfg).date()
    for offset in range(0, 8):
        day = today + timedelta(days=offset)
        if not day_is_active(cfg, day):
            continue
        start, _ = session_window(cfg, day)
        if start > ts:
            return start
    return None


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}:{s:02d}"
