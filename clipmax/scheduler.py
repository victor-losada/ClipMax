"""Arranque y cierre automáticos por horario + gestión de la sesión en vivo.

- A la hora de inicio (por defecto 15:00 de Colombia, días activos) se crean
  los grabadores, lectores de chat, el transcriptor en vivo y (si aplica) el
  lector de X, y se bloquea la suspensión de Windows.
- Al cierre (inicio + duración + margen) se detiene todo y, si está
  configurado, se lanza el post-proceso completo.
- Si ClipMax se reinicia a mitad del evento, retoma la sesión del día (las
  grabaciones siguen como partes nuevas del mismo día).
- Cada 5 minutos se re-calculan los candidatos para verlos en vivo en la web.
"""

from __future__ import annotations

import logging
import threading

from . import pipeline
from .chat import ChatListener
from .config import ConfigStore, active_streamers
from .liveclips import LiveClipper
from .db import Database
from .detector import run_detection
from .kick import KickClient
from .mentions import MentionMatcher
from .recorder import StreamRecorder
from .timeutil import current_window, fmt_clock, local_dt, next_window_start, now, today_str
from .transcriber import LiveTranscriber
from .winutil import KEEP_AWAKE
from .xcontext import XApiPoller

log = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, store: ConfigStore, db: Database):
        self.store = store
        self.db = db
        self.kick = KickClient()
        self._lock = threading.RLock()
        self.session: dict | None = None
        self.cfg: dict | None = None
        self.recorders: dict[str, StreamRecorder] = {}
        self.chats: dict[str, ChatListener] = {}
        self.live_tr: LiveTranscriber | None = None
        self.x_poller: XApiPoller | None = None
        self.clipper: LiveClipper | None = None
        self.end_ts: float | None = None
        self._last_detection = 0.0
        self._detecting = threading.Lock()
        self.manual_stops: set[str] = set()

    @property
    def active(self) -> bool:
        return self.session is not None

    def start(self, fecha: str | None = None, end_ts: float | None = None) -> dict:
        with self._lock:
            if self.session:
                return self.session
            cfg = self.store.get()
            streamers = active_streamers(cfg)
            if not streamers:
                raise RuntimeError("No hay streamers activos en la configuración")
            fecha = fecha or today_str(cfg)
            session = self.db.get_or_create_session(fecha)
            self.db.update_session(session["id"], estado="grabando",
                                   started_at=session["started_at"] or now(), ended_at=None)
            session = self.db.get_session(session["id"])
            matcher = MentionMatcher(cfg["streamers"])
            self.cfg, self.session = cfg, session
            self.recorders, self.chats = {}, {}
            for s in streamers:
                rec = StreamRecorder(cfg, self.db, session, s, self.kick)
                chat = ChatListener(cfg, self.db, session, s, self.kick, matcher)
                self.recorders[s["slug"]] = rec
                self.chats[s["slug"]] = chat
                rec.start()
                chat.start()
            self.live_tr = LiveTranscriber(cfg, self.db, session, self.recorders, matcher)
            self.live_tr.start()
            if cfg["x"]["modo"] == "api":
                self.x_poller = XApiPoller(cfg, self.db, session)
                self.x_poller.start()
            self.clipper = LiveClipper(cfg, self.db, session)
            self.clipper.start()
            if end_ts is None:
                win = current_window(cfg)
                end_ts = win[2] if win and win[0] == fecha else now() + float(cfg["evento"]["duracion_horas"]) * 3600
            self.end_ts = end_ts
            self.manual_stops.discard(fecha)
            KEEP_AWAKE.set_active(True)
            log.info("Sesión %s iniciada con %d streamers; cierre a las %s", fecha, len(streamers),
                     fmt_clock(end_ts, cfg, seconds=False))
            return session

    def stop(self, process: bool | None = None, manual: bool = False) -> dict | None:
        with self._lock:
            if not self.session:
                return None
            session, cfg = self.session, self.cfg
            log.info("Deteniendo la sesión %s…", session["fecha"])
            workers = [*self.recorders.values(), *self.chats.values(), self.live_tr, self.x_poller, self.clipper]
            # Marcamos "grabada" antes de esperar a los hilos: así el programador no la re-arranca.
            self.db.update_session(session["id"], estado="grabada", ended_at=now())
            if manual:
                self.manual_stops.add(session["fecha"])
            self.session = None
            self.recorders, self.chats, self.live_tr, self.x_poller = {}, {}, None, None
            self.clipper = None
            self.end_ts = None
        # Esperar a ffmpeg y compañía fuera del lock, para que la web siga respondiendo.
        for w in workers:
            if w:
                w.stop()
        for w in workers:
            if w and w.is_alive():
                w.join(timeout=40)
        KEEP_AWAKE.set_active(False)
        log.info("Sesión %s detenida", session["fecha"])
        if process is None:
            process = bool(cfg["evento"]["procesar_al_terminar"])
        if process:
            pipeline.run_async(self.store.get(), self.db, self.db.get_session(session["id"]))
        return session

    def request_clip(self, moment_id: int) -> bool:
        """Pide un clip en vivo de un candidato concreto (botón del Panel)."""
        with self._lock:
            if not self.clipper:
                return False
            self.clipper.request(moment_id)
            return True

    def force_clip(self, clip_id: int) -> bool:
        """"Publicar igual" un clip en vivo descartado. Con la grabación en curso va a la cola del
        armador de clips; si ya terminó, se arma en un hilo aparte."""
        c = self.db.live_clip(clip_id)
        if not c:
            return False
        with self._lock:
            if self.clipper and self.session and self.session["id"] == c["session_id"]:
                self.clipper.force(clip_id)
                return True
        session = self.db.get_session(c["session_id"])
        if not session:
            return False
        clipper = LiveClipper(self.store.get(), self.db, session)
        self.db.update_live_clip(clip_id, estado="procesando", nota="en cola")
        threading.Thread(target=clipper.publish, args=(clip_id,), name=f"clip-forzado-{clip_id}", daemon=True).start()
        return True

    def shutdown(self) -> None:
        """Al cerrar ClipMax: detiene todo pero deja la sesión en 'grabando' para retomarla
        si se vuelve a abrir dentro del horario."""
        with self._lock:
            session = self.session
        if not session:
            return
        self.stop(process=False)
        self.db.update_session(session["id"], estado="grabando")

    def tick(self) -> None:
        """Llamado periódicamente por el Scheduler."""
        if not self.session:
            return
        if self.end_ts and now() >= self.end_ts:
            log.info("Hora de cierre alcanzada")
            self.stop()
            return
        if now() - self._last_detection > 300 and not self._detecting.locked():
            self._last_detection = now()
            session, cfg = self.session, self.cfg

            def _detect():
                with self._detecting:
                    try:
                        run_detection(cfg, self.db, session)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Detección en vivo falló: %s", exc)

            threading.Thread(target=_detect, name="live-detect", daemon=True).start()

    def status(self) -> dict:
        cfg = self.store.get()
        out: dict = {"activa": self.active, "fecha": None, "cierre": None, "streamers": [],
                     "transcripcion_vivo": None, "x": None, "pipeline": pipeline.current(),
                     "procesando": pipeline.is_running()}
        nxt = next_window_start(cfg) if cfg["evento"]["programacion_activa"] else None
        out["proximo_inicio"] = fmt_clock(nxt, cfg, seconds=False) if nxt else None
        if nxt:
            out["proximo_inicio_fecha"] = local_dt(nxt, cfg).strftime("%Y-%m-%d")
        with self._lock:
            if self.session:
                out["fecha"] = self.session["fecha"]
                out["cierre"] = fmt_clock(self.end_ts, cfg, seconds=False) if self.end_ts else None
                for slug, rec in self.recorders.items():
                    chat = self.chats.get(slug)
                    out["streamers"].append({"slug": slug, "nombre": rec.streamer["nombre"],
                                             "grabacion": rec.status, "chat": chat.status if chat else {}})
                if self.live_tr:
                    out["transcripcion_vivo"] = self.live_tr.status
                if self.x_poller:
                    out["x"] = self.x_poller.status
                if self.clipper:
                    out["clips_vivo"] = self.clipper.status
        return out


class Scheduler(threading.Thread):
    def __init__(self, manager: SessionManager):
        super().__init__(name="scheduler", daemon=True)
        self.manager = manager
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        log.info("Programador activo")
        while not self._stop_evt.is_set():
            try:
                self._check()
            except Exception as exc:  # noqa: BLE001
                log.error("Error en el programador: %s", exc)
            self._stop_evt.wait(20)

    def _check(self) -> None:
        m = self.manager
        m.tick()
        cfg = m.store.get()
        if m.active or not cfg["evento"]["programacion_activa"]:
            return
        win = current_window(cfg)
        if not win:
            return
        fecha, _start, end = win
        if fecha in m.manual_stops:
            return
        sess = m.db.get_session_by_date(fecha)
        # Se retoma si la sesión nunca arrancó o quedó "grabando" (ClipMax se cerró a mitad).
        if sess is None or sess["estado"] in ("nueva", "grabando"):
            log.info("Ventana de grabación %s activa: arrancando", fecha)
            m.start(fecha, end)

