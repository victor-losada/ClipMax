"""Punto de entrada: python -m clipmax <comando>

Comandos:
  web                  interfaz web + grabación automática por horario (por defecto)
  grabar               graba ya, sin interfaz, hasta la hora de cierre o Ctrl+C
  procesar             post-proceso de un día (--fecha, --desde, --hasta)
  exportar-paquete     genera el .md para pegar en claude.ai (modo manual)
  importar-respuesta   importa el JSON de claude.ai y renderiza
  prompt-maestro       imprime el prompt maestro con tu configuración
  prompt-grok          imprime el prompt para que Grok investigue X del día
  doctor               verifica instalación (ffmpeg, whisper, API key, Kick…)
  descargar            baja whisper.cpp, modelos y (opcional) ffmpeg a bin/ y models/
  snapshot <slug>      captura un fotograma del directo (para calibrar la cámara)
  demo                 prueba de punta a punta con datos sintéticos (sin Kick)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time
import webbrowser

from .config import PROJECT_ROOT, ConfigError, ConfigStore, data_dir
from .logutil import setup_logging

log = logging.getLogger("clipmax")


def _load_env() -> None:
    env = PROJECT_ROOT / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env)
    except ImportError:  # parser mínimo por si falta python-dotenv
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _boot(config_path: str | None):
    from .db import Database

    _load_env()
    try:
        store = ConfigStore(config_path)
    except ConfigError as exc:
        print(f"Error en la configuración: {exc}")
        sys.exit(2)
    cfg = store.get()
    setup_logging(data_dir(cfg) / "logs")
    db = Database(data_dir(cfg) / "clipmax.db")
    return store, db


def _session(db, cfg, fecha: str | None):
    from .timeutil import today_str

    fecha = fecha or today_str(cfg)
    s = db.get_session_by_date(fecha)
    if not s:
        print(f"No hay sesión para {fecha}")
        sys.exit(1)
    return s


# ---------------------------------------------------------------------------
def cmd_web(args) -> None:
    from .scheduler import Scheduler, SessionManager
    from .web.app import create_app
    from .winutil import KEEP_AWAKE

    store, db = _boot(args.config)
    cfg = store.get()
    manager = SessionManager(store, db)
    scheduler = Scheduler(manager)
    KEEP_AWAKE.start()
    scheduler.start()
    app = create_app(store, db, manager)
    host, port = cfg["web"]["host"], int(cfg["web"]["puerto"])
    url = f"http://{host}:{port}"
    log.info("Interfaz web en %s (Ctrl+C para salir)", url)
    if cfg["web"]["abrir_navegador"] and not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        try:
            from waitress import serve

            serve(app, host=host, port=port, threads=8, _quiet=True)
        except ImportError:
            app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Cerrando ClipMax…")
        scheduler.stop()
        manager.shutdown()


def cmd_grabar(args) -> None:
    from .scheduler import SessionManager
    from .winutil import KEEP_AWAKE

    store, db = _boot(args.config)
    manager = SessionManager(store, db)
    KEEP_AWAKE.start()
    end = time.time() + args.horas * 3600 if args.horas else None
    manager.start(end_ts=end)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("Grabando. Ctrl+C para detener.")
    while not stop.is_set() and manager.active:
        manager.tick()
        stop.wait(10)
    manager.stop(process=not args.sin_procesar)
    from . import pipeline

    while pipeline.is_running():
        time.sleep(5)


def cmd_procesar(args) -> None:
    from .pipeline import Pipeline

    store, db = _boot(args.config)
    s = _session(db, store.get(), args.fecha)
    result = Pipeline(store.get(), db, s).run(args.desde, args.hasta)
    print(f"Resultado: {result}")


def cmd_exportar(args) -> None:
    from .pipeline import Pipeline

    store, db = _boot(args.config)
    s = _session(db, store.get(), args.fecha)
    path = Pipeline(store.get(), db, s).export_manual()
    print(f"Paquete listo: {path}\nCópialo completo en claude.ai y guarda la respuesta JSON.")


def cmd_importar(args) -> None:
    from .pipeline import Pipeline, import_claude_response

    store, db = _boot(args.config)
    cfg = store.get()
    s = _session(db, cfg, args.fecha)
    text = open(args.archivo, encoding="utf-8").read()
    decision, warnings = import_claude_response(cfg, db, s, text)
    for w in warnings:
        print("Aviso:", w)
    print(f"Decisión importada: {decision['titulo_video']}")
    if not args.no_render:
        print("Resultado:", Pipeline(cfg, db, db.get_session(s["id"])).run("editar", "reportar"))


def cmd_prompt(args) -> None:
    from .prompts import master_prompt

    _load_env()
    print(master_prompt(ConfigStore(args.config).get()))


def cmd_prompt_grok(args) -> None:
    from .prompts import grok_prompt
    from .timeutil import today_str

    _load_env()
    cfg = ConfigStore(args.config).get()
    print(grok_prompt(cfg, args.fecha or today_str(cfg)))


def cmd_snapshot(args) -> None:
    from .config import get_streamer
    from .kick import KickClient
    from .recorder import grab_snapshot

    store, _db = _boot(args.config)
    cfg = store.get()
    st = get_streamer(cfg, args.slug)
    if not st:
        print(f"No existe el streamer {args.slug} en la configuración")
        sys.exit(1)
    out = grab_snapshot(cfg, KickClient(), st, data_dir(cfg) / "snapshots" / f"{args.slug}.jpg")
    print(f"Captura guardada en {out}. Ajusta el recuadro en la web: /camara/{args.slug}")


def cmd_descargar(args) -> None:
    from .setup_tools import run

    # `--modelo` sin valores = ningún modelo (útil para bajar solo ffmpeg).
    models = ["base", "small"] if args.modelo is None else args.modelo
    if not run(models, args.ffmpeg, args.cuda, args.vad, args.sin_whisper, args.forzar):
        sys.exit(1)


def cmd_doctor(args) -> None:
    from . import tools
    from .config import active_streamers, resolve_path
    from .kick import KickClient

    _load_env()
    ok = True

    def line(status: bool | None, name: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "OK ", False: "ERR", None: "-- "}[status]
        if status is False:
            ok = False
        print(f"[{mark}] {name}: {detail}")

    line(sys.version_info >= (3, 10), "Python", sys.version.split()[0])
    try:
        store = ConfigStore(args.config)
        cfg = store.get()
        line(True, "Configuración", str(store.path if store.path.exists() else "usando config.example.yaml"))
    except ConfigError as exc:
        line(False, "Configuración", str(exc))
        return
    try:
        ff = tools.ffmpeg()
        line(True, "ffmpeg", f"{ff} · {tools.version_of([ff, '-version'])[:60]}")
    except tools.ToolMissing as exc:
        line(False, "ffmpeg", str(exc))
    line(bool(tools.ffprobe()) or None, "ffprobe", tools.ffprobe() or "no encontrado (opcional)")
    try:
        line(True, "yt-dlp", tools.version_of(tools.ytdlp_cmd() + ["--version"]))
    except tools.ToolMissing as exc:
        line(False, "yt-dlp", str(exc))
    try:
        import curl_cffi  # noqa: F401

        line(True, "curl_cffi", "instalado (necesario para la API de Kick)")
    except ImportError:
        line(False, "curl_cffi", "falta: pip install -r requirements.txt")
    try:
        line(True, "whisper-cli", tools.whisper_cli(cfg))
    except tools.ToolMissing as exc:
        line(False, "whisper-cli", str(exc))
    for key in ("modelo_vivo", "modelo_calidad"):
        p = resolve_path(cfg["transcripcion"][key])
        line(p.exists(), f"modelo whisper ({key})", str(p))
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if cfg["claude"]["modo"] == "api":
        line(bool(key), "ANTHROPIC_API_KEY", "configurada" if key else "falta en .env (o usa claude.modo: manual)")
    else:
        line(None, "ANTHROPIC_API_KEY", "modo manual: no se necesita")
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(cfg["evento"]["zona_horaria"])
        line(True, "Zona horaria", cfg["evento"]["zona_horaria"])
    except Exception as exc:  # noqa: BLE001
        line(False, "Zona horaria", f"{exc} (pip install tzdata)")
    streamers = active_streamers(cfg)
    line(bool(streamers), "Streamers activos", ", ".join(s["slug"] for s in streamers) or "ninguno")
    slugs = {s["slug"] for s in cfg["streamers"]}
    pair = cfg["evento"]["pareja_principal"]
    missing = [p for p in pair if p not in slugs]
    line(len(pair) == 2 and not missing, "Pareja principal",
         " ↔ ".join(pair) + (f" (no están en streamers: {', '.join(missing)})" if missing else ""))
    if not args.sin_red:
        kick = KickClient()
        for s in streamers[:40]:
            try:
                info = kick.get_channel(s["slug"], use_cache=False)
                line(True, f"Kick {s['slug']}", f"{'EN VIVO' if info.is_live else 'offline'} · chatroom {info.chatroom_id}")
            except Exception as exc:  # noqa: BLE001
                line(False, f"Kick {s['slug']}", str(exc)[:120])
    gb_h = {1080: 3.6, 720: 1.8, 480: 0.9, 360: 0.5}.get(int(cfg["grabacion"]["calidad_max"]), 2.0)
    need = gb_h * len(streamers) * (float(cfg["evento"]["duracion_horas"]) + 0.5)
    free = shutil.disk_usage(data_dir(cfg)).free / 1e9
    line(free > need * 1.3, "Disco", f"{free:.0f} GB libres; un día necesita ~{need:.0f} GB "
                                     f"({len(streamers)} streamers a {cfg['grabacion']['calidad_max']}p)")
    print("\nTodo listo." if ok else "\nHay problemas: revisa las líneas [ERR].")


def cmd_demo(args) -> None:
    from .demo import run_demo

    _load_env()
    if not (args.ver and args.sin_generar):
        run_demo(use_claude=args.con_claude, minutes=args.minutos)
    if args.ver:
        from .demo import DEMO_DIR

        cfg_path = DEMO_DIR / "config_demo.yaml"
        if not cfg_path.exists():
            print("Todavía no hay demo generada: ejecuta `python arrancar.py demo --ver` sin --sin-generar.")
            sys.exit(1)
        print("Abriendo la demo en http://127.0.0.1:5001 (Ctrl+C para cerrar)")
        cmd_web(argparse.Namespace(config=str(cfg_path), no_browser=False))


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # consola de Windows redirigida (cp1252) + tildes/emojis
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    from .netssl import configure_ssl

    configure_ssl()  # certificados HTTPS de Windows (ver netssl.py) antes de cualquier conexión
    parser = argparse.ArgumentParser(prog="python -m clipmax", description="ClipMax · clips del Desafío 4 en Kick")
    parser.add_argument("--config", help="ruta a config.yaml (por defecto, el de la raíz del proyecto)")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("web", help="interfaz web + horario automático")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("grabar", help="grabar ahora sin interfaz")
    p.add_argument("--horas", type=float, help="duración (por defecto la del evento)")
    p.add_argument("--sin-procesar", action="store_true")
    p.set_defaults(func=cmd_grabar)

    p = sub.add_parser("procesar", help="post-proceso de un día")
    p.add_argument("--fecha", help="AAAA-MM-DD (por defecto hoy)")
    p.add_argument("--desde", default="finalizar")
    p.add_argument("--hasta", default="reportar")
    p.set_defaults(func=cmd_procesar)

    p = sub.add_parser("exportar-paquete", help="paquete para claude.ai")
    p.add_argument("--fecha")
    p.set_defaults(func=cmd_exportar)

    p = sub.add_parser("importar-respuesta", help="importar JSON de claude.ai")
    p.add_argument("archivo")
    p.add_argument("--fecha")
    p.add_argument("--no-render", action="store_true")
    p.set_defaults(func=cmd_importar)

    p = sub.add_parser("prompt-maestro", help="imprime el prompt maestro")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("prompt-grok", help="imprime el prompt para que Grok investigue X del día")
    p.add_argument("--fecha", help="AAAA-MM-DD (por defecto hoy)")
    p.set_defaults(func=cmd_prompt_grok)

    p = sub.add_parser("doctor", help="verifica la instalación")
    p.add_argument("--sin-red", action="store_true", help="no consulta la API de Kick")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("descargar", help="descarga whisper.cpp, modelos y ffmpeg")
    p.add_argument("--modelo", nargs="*", help="modelos de whisper (base, small, medium, large-v3-turbo-q5_0…)")
    p.add_argument("--ffmpeg", action="store_true", help="descargar también ffmpeg (si no usas winget)")
    p.add_argument("--cuda", action="store_true", help="whisper con GPU NVIDIA (CUDA 12)")
    p.add_argument("--vad", action="store_true", help="modelo VAD Silero (mejores cortes de voz)")
    p.add_argument("--sin-whisper", action="store_true", help="solo modelos / ffmpeg")
    p.add_argument("--forzar", action="store_true", help="reinstalar whisper.cpp aunque ya exista")
    p.set_defaults(func=cmd_descargar)

    p = sub.add_parser("snapshot", help="captura del directo para calibrar la cámara")
    p.add_argument("slug")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("demo", help="prueba completa con datos sintéticos")
    p.add_argument("--con-claude", action="store_true", help="usar la API real en vez de una decisión de ejemplo")
    p.add_argument("--minutos", type=float, default=6, help="minutos de video sintético por streamer")
    p.add_argument("--ver", action="store_true", help="al terminar, abrir la demo en la web (puerto 5001)")
    p.add_argument("--sin-generar", action="store_true", help="con --ver: abrir la última demo sin regenerarla")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):  # sin comando = interfaz web
        args.func, args.no_browser = cmd_web, False
    args.func(args)


if __name__ == "__main__":
    main()
