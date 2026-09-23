"""Demo de punta a punta sin Kick: `python -m clipmax demo`.

Genera dos "streams" sintéticos (patrón de prueba + tono) con su chat y su
transcripción simulados, incluyendo un chipeo entre la pareja principal, y
corre todo el post-proceso: detección -> candidatos -> decisión -> video ->
reporte. Sirve para comprobar que ffmpeg, Pillow y la edición funcionan antes
del evento real. Con --con-claude usa la API de verdad (cuesta centavos).
"""

from __future__ import annotations

import json
import logging
import random
import shutil
import time
from pathlib import Path

import yaml

from . import brain, prompts, tools, xcontext
from .config import PROJECT_ROOT, ConfigStore
from .db import Database
from .logutil import setup_logging
from .mentions import MentionMatcher
from .pipeline import Pipeline
from .timeutil import today_str
from .transcriber import mention_signals_from_segments

log = logging.getLogger(__name__)

DEMO_DIR = PROJECT_ROOT / "data_demo"

# Guion sintético: (minuto, streamer, frase). Incluye menciones cruzadas.
DIALOGO = [
    (0.6, "westcol", "Bueno chat, arrancamos el día, hoy sí vamos por todo en el Desafío."),
    (0.9, "westcol", "Vamos a picar un poco, necesito hierro para la armadura."),
    (1.5, "gearofnos", "Chat, ¿ustedes vieron lo que hizo el Westcol ayer? Qué descaro."),
    (1.7, "gearofnos", "Ese man dice que es el mejor pero se murió con un creeper, jajaja."),
    (1.9, "westcol", "¿Quién está hablando de mí? ¿Gear of Nos? Parce, venga y me lo dice en la cara."),
    (2.05, "westcol", "Gear, usted es un llorón, le robé los diamantes en su cara y no hizo nada."),
    (2.15, "gearofnos", "Westcol, esos diamantes eran míos y lo sabe. Le voy a quemar la base hoy."),
    (2.3, "gearofnos", "Tranquilo que la venganza llega, chat, anoten la hora."),
    (3.2, "westcol", "Bueno, sigamos minando, qué pereza este túnel."),
    (4.0, "gearofnos", "Chat, acabo de hacer alianza con los de la aldea norte para ir contra el West."),
    (4.2, "gearofnos", "El que se meta con Westcol que se prepare, hoy lo sacamos del torneo."),
    (4.9, "westcol", "¿Alianza contra mí? Jajaja, que vengan todos, no les tengo miedo."),
    (5.2, "westcol", "Gear, se lo digo en serio, esta noche lo visito."),
]


def _make_video(path: Path, minutes: float, hue: int, label_color: str) -> None:
    """Video sintético 1280x720 con un recuadro que hace de 'cámara' arriba a la derecha."""
    dur = minutes * 60
    tools.run([
        tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={dur}",
        "-f", "lavfi", "-i", f"sine=frequency={300 + hue}:sample_rate=48000:duration={dur}",
        "-filter_complex",
        f"[0:v]hue=h={hue},drawbox=x=930:y=20:w=330:h=250:color={label_color}@0.9:t=fill[v];"
        "[1:a]volume=0.2[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", "-f", "mpegts", str(path),
    ], timeout=900)


def _demo_config(minutes: float, use_claude: bool) -> Path:
    base = PROJECT_ROOT / "config.example.yaml"
    raw = yaml.safe_load(base.read_text(encoding="utf-8")) if base.exists() else {}
    cam = {"x": 0.727, "y": 0.028, "w": 0.258, "h": 0.347}
    raw["streamers"] = [
        {"nombre": "Westcol", "url": "https://kick.com/westcol", "modo": "juego_cara", "prioridad": 1.5,
         "alias": ["west", "wescol", "güestcol"], "transcribir_en_vivo": True, "camara": cam},
        {"nombre": "Gear of Nos", "url": "https://kick.com/gearofnos", "modo": "cara", "prioridad": 1.5,
         "alias": ["gear", "gear of nos", "gearofnos"], "transcribir_en_vivo": True, "camara": cam},
    ]
    raw.setdefault("evento", {})["pareja_principal"] = ["westcol", "gearofnos"]
    raw["evento"]["programacion_activa"] = False
    raw.setdefault("grabacion", {}).update({"carpeta_datos": str(DEMO_DIR), "borrar_partes_ts": True})
    raw.setdefault("deteccion", {}).update({"ventana_base_min": 1.5, "candidatos_max": 8})
    raw.setdefault("edicion", {}).update({
        "ancho": 1280, "alto": 720, "preset": "ultrafast", "duracion_min_min": 1,
        "duracion_max_min": max(2, int(minutes)), "narracion": "tarjetas", "codec": "libx264",
    })
    raw.setdefault("claude", {})["modo"] = "api" if use_claude else "manual"
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    path = DEMO_DIR / "config_demo.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _synthetic_chat(db: Database, sid: int, slug: str, t0: float, minutes: float, spikes: list[float],
                    other_slug: str, other: str, rnd: random.Random) -> None:
    """Chat con ritmo base ~9 msg/5 s y explosiones en los minutos `spikes` que nombran al otro."""
    buckets, mentions, msgs = [], [], []
    end = t0 + minutes * 60
    t = t0 - t0 % 5
    while t < end:
        minute = (t - t0) / 60
        n = max(0, int(rnd.gauss(9, 2.5)))
        hype = rnd.randint(0, 2)
        ment = 0
        for sp in spikes:
            if 0 <= minute - sp < 0.35:  # el chat explota ~20 s
                n += rnd.randint(35, 60)
                hype += rnd.randint(20, 35)
                ment += rnd.randint(4, 9)
                for _ in range(8):
                    msgs.append((sid, slug, t + rnd.random() * 5, "fan" + str(rnd.randint(1, 999)),
                                 rnd.choice(["JAJAJAJA", f"{other} lo humilló", "CLIP IT", "KEKW", "NOOO",
                                             f"@{other} responde", "se picó", "💀💀💀"])))
        buckets.append((sid, slug, t, n, max(1, n - 2), hype))
        if ment:
            mentions.append((sid, slug, t, other_slug, ment))
        t += 5
    db.add_chat_buckets(buckets)
    db.add_chat_mentions(mentions)
    db.add_chat_messages(msgs)


def _canned_decision(candidates: list[dict]) -> dict:
    """Decisión de ejemplo (lo que haría Claude) para probar sin gastar API."""
    top = candidates[:4]
    guion = [{"tipo": "narracion", "texto": "Día de prueba en el Desafío: el chipeo entre Westcol y Gear of Nos no tardó en llegar.",
              "candidato_id": 0, "inicio": 0, "fin": 0, "titulo_en_pantalla": "", "prioridad": 5, "motivo": "gancho"}]
    for i, c in enumerate(top, 1):
        guion.append({"tipo": "narracion", "texto": f"Mientras tanto, en el stream de {c['nombre']}…",
                      "candidato_id": 0, "inicio": 0, "fin": 0, "titulo_en_pantalla": "", "prioridad": 3, "motivo": ""})
        guion.append({"tipo": "clip", "texto": "", "candidato_id": c["id"], "inicio": 3.0,
                      "fin": min(c["duracion"] - 1, 55.0), "titulo_en_pantalla": f"MOMENTO {i}",
                      "prioridad": 5 - min(i, 4), "motivo": c["por_que"]})
    guion.append({"tipo": "narracion", "texto": "¿Cumplirá Gear su amenaza de quemar la base? Mañana lo sabremos.",
                  "candidato_id": 0, "inicio": 0, "fin": 0, "titulo_en_pantalla": "", "prioridad": 5, "motivo": "cierre"})
    mejores = [{"candidato_id": c["id"], "inicio": 3.0, "fin": min(c["duracion"] - 1, 45.0),
                "titulo": f"Chipeo #{i}", "por_que_importa": "Ejemplo generado por la demo.",
                "captions_tiktok": ["Westcol no se aguantó 😂", "Gear lo dijo en vivo 👀", "Esto no termina aquí"],
                "hashtags": ["#Desafio4", "#Westcol", "#Kick"]} for i, c in enumerate(top[:2], 1)]
    return {"titulo_video": "Demo · El robo de los diamantes", "resumen_del_dia": "Resumen de demostración.",
            "lore_para_manana": "Gear amenazó con quemar la base de Westcol.", "guion": guion,
            "mejores_momentos": mejores, "descartados": [], "notas_editor": "Decisión sintética de la demo."}


def run_demo(use_claude: bool = False, minutes: float = 6.0) -> None:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    cfg_path = _demo_config(minutes, use_claude)
    store = ConfigStore(cfg_path)
    cfg = store.get()
    setup_logging(DEMO_DIR / "logs")
    db = Database(DEMO_DIR / "clipmax.db")
    fecha = today_str(cfg)
    session = db.get_or_create_session(fecha)
    sid = session["id"]
    rnd = random.Random(4)
    t0 = time.time() - minutes * 60 - 60

    print(f"1/5 Generando {minutes:.0f} min de video sintético por streamer…")
    rec_dir = DEMO_DIR / "sesiones" / fecha / "grabaciones"
    for i, (slug, hue, color) in enumerate([("westcol", 0, "red"), ("gearofnos", 120, "blue")]):
        d = rec_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{slug}_parte001.ts"
        _make_video(path, minutes, hue, color)
        part = db.add_part(sid, slug, str(path), t0 + i * 0.4)
        db.close_part(part["id"], t0 + i * 0.4 + minutes * 60, path.stat().st_size)

    print("2/5 Simulando chat, transcripción en vivo y contexto de X…")
    _synthetic_chat(db, sid, "westcol", t0, minutes, [1.95, 4.95], "gearofnos", "gear", rnd)
    _synthetic_chat(db, sid, "gearofnos", t0, minutes, [2.2, 4.1], "westcol", "westcol", rnd)
    matcher = MentionMatcher(cfg["streamers"])
    for slug in ("westcol", "gearofnos"):
        segs = [(t0 + m * 60, t0 + m * 60 + 5.5, txt) for m, s, txt in DIALOGO if s == slug]
        db.add_segments(sid, slug, segs, "vivo")
        mention_signals_from_segments(db, sid, slug, segs, matcher)
    xcontext.save_manual_context(db, session, (
        "@fan1: Westcol le robó los diamantes a Gear of Nos en pleno Desafío 4 jajaja\n\n"
        "@fan2: Gear dice que le va a quemar la base a Westcol esta noche 🔥\n\n"
        "@fan3: la alianza contra Westcol es lo mejor del Desafío 4"))

    print("3/5 Post-proceso: MP4 final, detección y puntuación…")
    session = db.get_session(sid)
    pipe = Pipeline(cfg, db, session)
    for desde, hasta in (("finalizar", "detectar"), ("contexto_x", "puntuar")):
        if pipe.run(desde, hasta) != "ok":
            print("Falló el post-proceso; revisa", DEMO_DIR / "logs" / "clipmax.log")
            return

    print("4/5 Decisión editorial…")
    if use_claude:
        result = pipe.run("decidir", "decidir")
        if result != "ok":
            print("La llamada a Claude no se completó; revisa el log.")
            return
    else:
        candidates, _material = pipe._material()
        decision, warnings = brain.validate_decision(cfg, _canned_decision(candidates), candidates)
        brain.save_decision(cfg, db, session, decision, "demo")
        pkg = pipe.export_manual()
        print(f"   Paquete para claude.ai generado (para que veas cómo luce): {pkg}")

    print("5/5 Editando video y reporte…")
    if pipe.run("editar", "reportar") != "ok":
        print("Falló la edición; revisa", DEMO_DIR / "logs" / "clipmax.log")
        return
    folder = DEMO_DIR / "sesiones" / fecha
    print("\nListo:")
    for p in sorted(folder.glob("resumen_*")) + sorted((folder / "clips_tiktok").glob("*.mp4")):
        print("  ", p)
    print(json.dumps({"candidatos": len(prompts.load_candidates(cfg, fecha))}, ensure_ascii=False))
