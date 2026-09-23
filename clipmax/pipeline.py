"""Post-proceso del día, por pasos reanudables.

  finalizar   -> partes .ts => un MP4 por streamer
  detectar    -> picos de chat, menciones, sincronía => momentos candidatos
  transcribir -> whisper.cpp (modelo de calidad) sobre cada candidato
  contexto_x  -> importa archivos de X / búsqueda web con Claude (según x.modo)
  puntuar     -> re-puntúa con transcripción + temas de X
  decidir     -> Claude (API) o paquete para claude.ai (modo manual)
  editar      -> ffmpeg arma el resumen + clips verticales para TikTok
  reportar    -> resumen_<fecha>.md / .html

Cada paso deja su estado en sessions.pipeline_json, así la web muestra el
progreso y se puede re-ejecutar desde cualquier paso (p. ej. re-editar tras
cambiar el formato, sin volver a pagar a Claude).
"""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path

from . import brain, detector, editor, prompts, report, tools, transcriber, xcontext
from .config import session_dir
from .db import Database
from .mentions import MentionMatcher
from .recorder import finalize_recordings

log = logging.getLogger(__name__)

STEPS = ["finalizar", "detectar", "transcribir", "contexto_x", "puntuar", "decidir", "editar", "reportar"]
_LOCK = threading.Lock()
_current: dict = {"fecha": None, "paso": None, "detalle": ""}


class WaitingForClaude(Exception):
    """El paso 'decidir' quedó esperando la respuesta pegada desde claude.ai."""


def is_running() -> bool:
    return _LOCK.locked()


def current() -> dict:
    return dict(_current)


class Pipeline:
    def __init__(self, cfg: dict, db: Database, session: dict):
        self.cfg = cfg
        self.db = db
        self.session = session
        self.sid = session["id"]
        self.step = ""

    # -- utilidades ---------------------------------------------------------------
    def progress(self, detail: str) -> None:
        _current.update(detalle=detail)
        self.db.set_pipeline_step(self.sid, self.step, "en_curso", detail)

    def _matcher(self) -> MentionMatcher:
        return MentionMatcher(self.cfg["streamers"])

    def _x_keywords(self) -> list[tuple[str, int]]:
        texts = [p["texto"] for p in self.db.x_posts(self.sid)]
        sess = self.db.get_session(self.sid)
        if sess and sess["x_contexto"]:
            texts.append(sess["x_contexto"])
        return xcontext.keywords(texts)

    # -- pasos --------------------------------------------------------------------
    def finalizar(self) -> str:
        res = finalize_recordings(self.cfg, self.db, self.session)
        return f"{sum(len(v) for v in res.values())} archivo(s) MP4"

    def detectar(self) -> str:
        moments = detector.run_detection(self.cfg, self.db, self.session)
        return f"{len(moments)} candidatos"

    def transcribir(self) -> str:
        moments = self.db.moments(self.sid, limit=int(self.cfg["deteccion"]["candidatos_max"]))
        try:
            n = transcriber.transcribe_moments(self.cfg, self.db, self.session, moments, self.progress)
        except tools.ToolMissing as exc:
            # Sin whisper se sigue: Claude recibe el chat y lo que haya del transcriptor en vivo.
            log.warning("Transcripción omitida: %s", exc)
            return f"omitido ({exc})"
        return f"{n} candidatos transcritos"

    def contexto_x(self) -> str:
        n = xcontext.import_folder(self.cfg, self.db, self.session)
        msg = f"{n} posts importados de archivos"
        if self.cfg["x"]["modo"] == "claude_web" and self.cfg["claude"]["modo"] == "api":
            already = [p for p in self.db.x_posts(self.sid) if p["fuente"] == "claude_web"]
            if not already:
                try:
                    brain.research_x_web(self.cfg, self.db, self.session)
                    msg += "; resumen web de Claude agregado"
                except (brain.BudgetExceeded, brain.ClaudeError) as exc:
                    log.warning("Sin búsqueda web de X: %s", exc)
                    msg += f"; búsqueda web omitida ({exc})"
        return msg

    def puntuar(self) -> str:
        kw = [k for k, _ in self._x_keywords()]
        bursts = xcontext.burst_times(self.db, self.session)
        moments = detector.rescore_with_transcripts(self.cfg, self.db, self.session, self._matcher(), kw, bursts)
        pair = sum(1 for m in moments if m["componentes"].get("pareja"))
        return f"{len(moments)} candidatos re-puntuados ({pair} con la pareja principal)"

    def _material(self) -> tuple[list[dict], str]:
        candidates = prompts.build_candidates(self.cfg, self.db, self.session)
        if not candidates:
            raise RuntimeError("No hay candidatos: ¿hubo grabación y chat este día?")
        prompts.save_candidates(self.cfg, self.session["fecha"], candidates)
        sess = self.db.get_session(self.sid)
        x_text = xcontext.context_text(self.db, sess, self.cfg)
        material = prompts.build_day_material(self.cfg, self.db, sess, candidates, x_text, self._x_keywords())
        folder = brain.claude_dir(self.cfg, self.session["fecha"])
        (folder / "material_del_dia.md").write_text(material, encoding="utf-8")
        return candidates, material

    def export_manual(self) -> Path:
        _candidates, material = self._material()
        path = brain.claude_dir(self.cfg, self.session["fecha"]) / f"paquete_para_claude_{self.session['fecha']}.md"
        path.write_text(prompts.manual_package(self.cfg, material), encoding="utf-8")
        self.db.add_output(self.sid, "paquete", str(path))
        return path

    def decidir(self) -> str:
        if self.cfg["claude"]["modo"] == "manual":
            path = self.export_manual()
            raise WaitingForClaude(f"Paquete listo para pegar en claude.ai: {path.name}")
        candidates, material = self._material()
        try:
            raw = brain.decide_api(self.cfg, self.db, self.session, material)
        except (brain.BudgetExceeded, brain.ClaudeError) as exc:
            # Sin presupuesto, sin clave o con la API caída: el día no se pierde, queda el modo manual.
            # (Para reintentar la API, re-ejecuta el paso "decidir" desde la web.)
            path = brain.claude_dir(self.cfg, self.session["fecha"]) / f"paquete_para_claude_{self.session['fecha']}.md"
            path.write_text(prompts.manual_package(self.cfg, material), encoding="utf-8")
            self.db.add_output(self.sid, "paquete", str(path))
            raise WaitingForClaude(f"{exc} · Paquete manual listo: {path.name}") from exc
        decision, warnings = brain.validate_decision(self.cfg, raw, candidates)
        brain.save_decision(self.cfg, self.db, self.session, decision, "api")
        for w in warnings:
            log.warning("Decisión: %s", w)
        clips = sum(1 for g in decision["guion"] if g["tipo"] == "clip")
        return f"{clips} clips elegidos, {len(decision['mejores_momentos'])} mejores momentos"

    def editar(self) -> str:
        decision = brain.load_decision(self.cfg, self.session["fecha"])
        if not decision:
            raise RuntimeError("No hay decision.json; ejecuta el paso 'decidir' o importa la respuesta de Claude")
        candidates = prompts.load_candidates(self.cfg, self.session["fecha"])
        video = editor.render_summary(self.cfg, self.db, self.session, decision, candidates, self.progress)
        msg = f"video {video.name}"
        if self.cfg["edicion"]["exportar_clips_tiktok"]:
            clips = editor.export_tiktok_clips(self.cfg, self.db, self.session, decision, candidates, self.progress)
            msg += f" + {len(clips)} clips TikTok"
        return msg

    def reportar(self) -> str:
        fecha = self.session["fecha"]
        decision = brain.load_decision(self.cfg, fecha)
        if not decision:
            raise RuntimeError("No hay decision.json")
        candidates = prompts.load_candidates(self.cfg, fecha)
        video = session_dir(self.cfg, fecha) / f"resumen_{fecha}.mp4"
        tiktok = sorted((session_dir(self.cfg, fecha) / "clips_tiktok").glob("*.mp4"))
        md, _html = report.write_report(self.cfg, self.db, self.session, decision, candidates,
                                        video if video.exists() else None, tiktok)
        return md.name

    # -- ejecución ------------------------------------------------------------------
    def run(self, desde: str = "finalizar", hasta: str = "reportar") -> str:
        if desde not in STEPS or hasta not in STEPS:
            raise ValueError(f"Paso inválido. Usa: {', '.join(STEPS)}")
        steps = STEPS[STEPS.index(desde): STEPS.index(hasta) + 1]
        if not _LOCK.acquire(blocking=False):
            raise RuntimeError("Ya hay un procesamiento en curso")
        _current.update(fecha=self.session["fecha"], paso=None, detalle="")
        self.db.update_session(self.sid, estado="procesando")
        try:
            for step in steps:
                self.step = step
                _current.update(paso=step, detalle="")
                self.db.set_pipeline_step(self.sid, step, "en_curso")
                log.info("[%s] paso %s…", self.session["fecha"], step)
                try:
                    detail = getattr(self, step)()
                except WaitingForClaude as wait:
                    self.db.set_pipeline_step(self.sid, step, "esperando", str(wait))
                    self.db.update_session(self.sid, estado="esperando_claude")
                    log.info("%s", wait)
                    return "esperando_claude"
                self.db.set_pipeline_step(self.sid, step, "ok", detail)
                log.info("[%s] %s: %s", self.session["fecha"], step, detail)
            self.db.update_session(self.sid, estado="lista" if steps[-1] == "reportar" else "procesando")
            return "ok"
        except Exception as exc:  # noqa: BLE001
            log.error("Paso %s falló: %s\n%s", self.step, exc, traceback.format_exc())
            self.db.set_pipeline_step(self.sid, self.step, "error", str(exc)[:500])
            self.db.update_session(self.sid, estado="error")
            return "error"
        finally:
            _current.update(paso=None, detalle="")
            _LOCK.release()


def run_async(cfg: dict, db: Database, session: dict, desde: str = "finalizar", hasta: str = "reportar") -> bool:
    if is_running():
        return False
    t = threading.Thread(target=lambda: Pipeline(cfg, db, session).run(desde, hasta),
                         name=f"pipeline-{session['fecha']}", daemon=True)
    t.start()
    return True


def import_claude_response(cfg: dict, db: Database, session: dict, text: str) -> tuple[dict, list[str]]:
    """Modo manual: valida y guarda la respuesta pegada desde claude.ai."""
    candidates = prompts.load_candidates(cfg, session["fecha"])
    if not candidates:
        raise brain.ClaudeError("No hay candidatos exportados para este día; genera primero el paquete")
    raw = brain.extract_json(text)
    decision, warnings = brain.validate_decision(cfg, raw, candidates)
    brain.save_decision(cfg, db, session, decision, "manual")
    db.set_pipeline_step(session["id"], "decidir", "ok", "respuesta importada desde claude.ai")
    return decision, warnings
