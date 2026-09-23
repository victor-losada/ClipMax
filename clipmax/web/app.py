"""Interfaz web local (http://127.0.0.1:5000).

Solo escucha en localhost: no hay usuarios ni contraseñas porque nada sale de
tu PC. Desde aquí configuras streamers y horario, ves el estado en vivo,
pegas el contexto de X, lanzas el procesamiento y descargas los resultados.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from .. import brain, pipeline, prompts, xcontext
from ..config import ConfigError, ConfigStore, data_dir, get_streamer, session_dir, streamer_name
from ..db import Database
from ..detector import describe_components
from ..logutil import RING
from ..recorder import grab_snapshot, snapshot_from_file
from ..scheduler import SessionManager
from ..timeutil import fmt_clock, fmt_duration, today_str

log = logging.getLogger(__name__)


def create_app(store: ConfigStore, db: Database, manager: SessionManager) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    @app.context_processor
    def _globals():
        cfg = store.get()
        return {"evento": cfg["evento"]["nombre"], "cfg": cfg}

    @app.template_filter("clock")
    def _clock(ts):
        return fmt_clock(ts, store.get()) if ts else "—"

    @app.template_filter("dur")
    def _dur(sec):
        return fmt_duration(sec or 0)

    def _session_or_404(fecha: str) -> dict:
        s = db.get_session_by_date(fecha)
        if not s:
            abort(404, f"No hay sesión para {fecha}")
        return s

    def _err(msg: str, code: int = 400):
        return jsonify({"ok": False, "error": msg}), code

    # ------------------------------------------------------------------ páginas
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/config")
    def config_page():
        return render_template("config.html", cfg_json=json.dumps(store.get(), ensure_ascii=False))

    @app.get("/config/yaml")
    def config_yaml():
        text = yaml.safe_dump(store.get(), allow_unicode=True, sort_keys=False, width=100)
        return render_template("config_yaml.html", yaml_text=text)

    @app.get("/sesiones")
    def sessions_page():
        return render_template("sesiones.html", sesiones=db.list_sessions(), hoy=today_str(store.get()))

    @app.get("/sesion/<fecha>")
    def session_page(fecha: str):
        cfg = store.get()
        # La sesión de hoy se crea al abrirla: así se puede pegar el contexto de X antes de grabar.
        s = db.get_or_create_session(fecha) if fecha == today_str(cfg) else _session_or_404(fecha)
        moments = db.moments(s["id"], limit=int(cfg["deteccion"]["candidatos_max"]))
        for m in moments:
            m["por_que"] = describe_components(cfg, m["componentes"])
            m["transcripcion"] = db.segments(s["id"], m["slug"], m["start_ts"], m["end_ts"])
            m["nombre"] = (get_streamer(cfg, m["slug"]) or {}).get("nombre", m["slug"])
        outputs = db.outputs(s["id"])
        folder = session_dir(cfg, fecha)
        files = {
            "video": (folder / f"resumen_{fecha}.mp4").exists(),
            "html": (folder / f"resumen_{fecha}.html").exists(),
            "md": (folder / f"resumen_{fecha}.md").exists(),
            "paquete": (folder / "claude" / f"paquete_para_claude_{fecha}.md").exists(),
            "tiktok": sorted(p.name for p in (folder / "clips_tiktok").glob("*.mp4")) if (folder / "clips_tiktok").exists() else [],
        }
        parts = db.list_parts(s["id"])
        return render_template(
            "sesion.html", s=s, fecha=fecha, moments=moments, outputs=outputs, files=files,
            pasos=pipeline.STEPS, estado_pasos=db.pipeline_state(s["id"]), parts=parts,
            x_posts=db.x_posts(s["id"]), decision=brain.load_decision(cfg, fecha),
            runs=db.query("SELECT * FROM claude_runs WHERE session_id=? ORDER BY created_at DESC", (s["id"],)),
        )

    @app.get("/camara/<slug>")
    def camera_page(slug: str):
        cfg = store.get()
        st = get_streamer(cfg, slug)
        if not st:
            abort(404)
        snap = data_dir(cfg) / "snapshots" / f"{slug}.jpg"
        return render_template("camara.html", st=st, has_snap=snap.exists())

    # ------------------------------------------------------------------ API: estado y sesión
    @app.get("/api/estado")
    def api_status():
        cfg = store.get()
        st = manager.status()
        st["presupuesto"] = brain.budget_status(cfg, db)
        st["log"] = RING.tail(60)
        st["modo_claude"] = cfg["claude"]["modo"]
        fecha = st.get("fecha") or today_str(cfg)
        sess = db.get_session_by_date(fecha)
        st["hoy"] = fecha
        st["momentos"] = []
        if sess:
            st["estado_sesion"] = sess["estado"]
            for m in db.moments(sess["id"], limit=10):
                st["momentos"].append({
                    "rank": m["rank"], "slug": m["slug"], "nombre": streamer_name(cfg, m["slug"]), "score": m["score"],
                    "desde": fmt_clock(m["start_ts"], cfg), "hasta": fmt_clock(m["end_ts"], cfg),
                    "por_que": describe_components(cfg, m["componentes"]),
                    "pareja": bool(m["componentes"].get("pareja")),
                })
        return jsonify(st)

    @app.post("/api/sesion/iniciar")
    def api_start():
        try:
            s = manager.start()
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))
        return jsonify({"ok": True, "fecha": s["fecha"]})

    @app.post("/api/sesion/detener")
    def api_stop():
        body = request.get_json(silent=True) or {}
        s = manager.stop(process=bool(body.get("procesar", True)), manual=True)
        return jsonify({"ok": True, "fecha": s["fecha"] if s else None})

    @app.post("/api/sesion/<fecha>/procesar")
    def api_process(fecha: str):
        s = _session_or_404(fecha)
        body = request.get_json(silent=True) or {}
        desde = body.get("desde", "finalizar")
        hasta = body.get("hasta", "reportar")
        if manager.active and manager.session and manager.session["fecha"] == fecha and desde == "finalizar":
            return _err("La sesión sigue grabando; detenla primero o procesa desde 'detectar'.")
        if not pipeline.run_async(store.get(), db, s, desde, hasta):
            return _err("Ya hay un procesamiento en curso")
        return jsonify({"ok": True})

    @app.post("/api/sesion/<fecha>/x")
    def api_x(fecha: str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
            return _err("fecha inválida (usa AAAA-MM-DD)")
        s = db.get_or_create_session(fecha)
        text = (request.get_json(silent=True) or {}).get("texto", "")
        n = xcontext.save_manual_context(db, s, text)
        kw = xcontext.keywords([p["texto"] for p in db.x_posts(s["id"])])
        return jsonify({"ok": True, "posts": n, "temas": kw[:15]})

    @app.post("/api/sesion/<fecha>/exportar")
    def api_export(fecha: str):
        s = _session_or_404(fecha)
        try:
            path = pipeline.Pipeline(store.get(), db, s).export_manual()
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))
        return jsonify({"ok": True, "archivo": path.name, "texto": path.read_text(encoding="utf-8")})

    @app.post("/api/sesion/<fecha>/importar")
    def api_import(fecha: str):
        s = _session_or_404(fecha)
        body = request.get_json(silent=True) or {}
        try:
            decision, warnings = pipeline.import_claude_response(store.get(), db, s, body.get("texto", ""))
        except brain.ClaudeError as exc:
            return _err(str(exc))
        started = False
        if body.get("renderizar", True):
            started = pipeline.run_async(store.get(), db, db.get_session(s["id"]), "editar", "reportar")
        return jsonify({"ok": True, "advertencias": warnings, "render": started,
                        "clips": sum(1 for g in decision["guion"] if g["tipo"] == "clip")})

    @app.get("/api/prompt-maestro")
    def api_master_prompt():
        return jsonify({"texto": prompts.master_prompt(store.get())})

    # ------------------------------------------------------------------ API: configuración
    @app.post("/api/config")
    def api_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _err("JSON inválido")
        try:
            store.save(body)
        except (ConfigError, ValueError, TypeError) as exc:
            return _err(str(exc))
        return jsonify({"ok": True, "aviso": "Guardado. Los cambios aplican a la próxima sesión."
                        if manager.active else "Guardado."})

    @app.post("/config/yaml")
    def api_config_yaml():
        try:
            data = yaml.safe_load(request.form.get("yaml", "")) or {}
            store.save(data)
        except (yaml.YAMLError, ConfigError, ValueError, TypeError) as exc:
            return render_template("config_yaml.html", yaml_text=request.form.get("yaml", ""), error=str(exc))
        return redirect(url_for("config_yaml"))

    @app.post("/api/camara/<slug>/snapshot")
    def api_snapshot(slug: str):
        cfg = store.get()
        st = get_streamer(cfg, slug)
        if not st:
            return _err("streamer desconocido", 404)
        out = data_dir(cfg) / "snapshots" / f"{slug}.jpg"
        try:
            grab_snapshot(cfg, manager.kick, st, out)
        except Exception as live_exc:  # noqa: BLE001
            # Sin directo: usamos la última grabación disponible.
            parts = db.query("SELECT * FROM recordings WHERE slug=? ORDER BY started_at DESC LIMIT 1", (slug,))
            src = None
            if parts:
                p = parts[0]
                src = p["final_path"] if p["final_path"] and Path(p["final_path"]).exists() else p["path"]
            if not src or not Path(src).exists():
                return _err(f"No está en vivo y no hay grabaciones: {live_exc}")
            try:
                snapshot_from_file(src, 30.0, out)
            except Exception as exc:  # noqa: BLE001
                return _err(str(exc))
        return jsonify({"ok": True})

    @app.get("/api/camara/<slug>/imagen")
    def api_snapshot_img(slug: str):
        path = data_dir(store.get()) / "snapshots" / f"{slug}.jpg"
        if not path.exists():
            abort(404)
        return send_file(path, max_age=0)

    @app.post("/api/camara/<slug>")
    def api_camera(slug: str):
        cfg = store.get()
        body = request.get_json(silent=True) or {}
        for st in cfg["streamers"]:
            if st["slug"] == slug:
                st["camara"] = None if body.get("borrar") else {k: round(float(body[k]), 4) for k in ("x", "y", "w", "h")}
                break
        else:
            return _err("streamer desconocido", 404)
        try:
            store.save(cfg)
        except ConfigError as exc:
            return _err(str(exc))
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ archivos
    @app.get("/archivos/<fecha>/<path:rel>")
    def files(fecha: str, rel: str):
        base = session_dir(store.get(), fecha).resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            abort(403)
        if not target.is_file():
            abort(404)
        return send_file(target, conditional=True)

    return app
