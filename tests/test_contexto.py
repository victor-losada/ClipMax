"""Contexto del día: prompt para Grok, pegado de su salida, pausa opcional y días sin datos."""

import pytest

from clipmax import pipeline
from clipmax.pipeline import Pipeline
from clipmax.prompts import grok_prompt
from clipmax.scheduler import SessionManager
from clipmax.web.app import create_app
from clipmax.xcontext import parse_manual_text


def test_grok_prompt_is_filled(cfg):
    cfg["x"]["cuentas"] = ["dedreviil", "dedsafio"]
    text = grok_prompt(cfg, "2026-09-24")
    assert "{{" not in text
    assert "Westcol" in text and "Gear of Nos" in text and "@dedreviil, @dedsafio" in text
    assert "2026-09-24, desde las 14:00 hasta las 23:15 (America/Bogota)" in text
    assert "Otro (kick.com/otro)" in text


def test_parse_grok_output_format():
    posts = parse_manual_text(
        "@dedreviil (19:40): anuncia gulag para el viernes. https://x.com/dedreviil/status/123\n\n"
        "@fan_west (20:05): Westcol le respondió a Gear en vivo\n\n"
        "PIQUES: Westcol vs Gear por los diamantes")
    assert posts[0]["autor"] == "@dedreviil" and posts[0]["ext_id"] == "x123"
    assert posts[0]["texto"].startswith("anuncia gulag")
    assert posts[1]["autor"] == "@fan_west" and posts[1]["texto"] == "Westcol le respondió a Gear en vivo"
    assert posts[2]["texto"].startswith("PIQUES")


def test_empty_day_is_sin_datos_not_error(cfg, db):
    s = db.get_or_create_session("2026-09-24")
    assert Pipeline(cfg, db, s).run() == "sin_datos"
    assert db.get_session(s["id"])["estado"] == "sin_datos"
    step = db.pipeline_state(s["id"])["finalizar"]
    assert step["estado"] == "sin_datos" and "Iniciar ahora" in step["detalle"]


def test_waits_for_x_context_before_claude(cfg, db):
    cfg["x"]["esperar_contexto"] = True
    s = db.get_or_create_session("2026-09-24")
    assert Pipeline(cfg, db, s).run("decidir", "decidir") == "esperando_contexto"
    assert db.get_session(s["id"])["estado"] == "esperando_contexto"


def test_pasting_context_resumes_pipeline(store, db, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_async", lambda cfg, db_, sess, desde="finalizar", hasta="reportar":
                        calls.append((sess["fecha"], desde, hasta)) or True)
    s = db.get_or_create_session("2026-09-24")
    db.update_session(s["id"], estado="esperando_contexto")
    app = create_app(store, db, SessionManager(store, db))
    r = app.test_client().post("/api/sesion/2026-09-24/x", json={"texto": "@a (19:00): Westcol vs Gear"})
    assert r.get_json()["reanudado"] is True
    assert calls == [("2026-09-24", "decidir", "reportar")]
    # Si no estaba esperando, guardar contexto no dispara nada.
    db.update_session(s["id"], estado="grabando")
    assert app.test_client().post("/api/sesion/2026-09-24/x", json={"texto": "x"}).get_json()["reanudado"] is False


def test_grok_prompt_endpoint(store, db):
    app = create_app(store, db, SessionManager(store, db))
    c = app.test_client()
    assert "VENTANA" in c.get("/api/prompt-grok/2026-09-24").get_json()["texto"]
    assert c.get("/api/prompt-grok/hoy").status_code == 400


@pytest.mark.parametrize("value", ["@dedreviil, dedsafio", ["@dedreviil", " dedsafio "]])
def test_config_normalizes_accounts(value):
    from clipmax.config import DEFAULTS, deep_merge, validate

    cfg = validate(deep_merge(DEFAULTS, {"x": {"cuentas": value}}))
    assert cfg["x"]["cuentas"] == ["dedreviil", "dedsafio"]


# -- memoria entre días ---------------------------------------------------------------
GROK_AYER = """@dedreviil (19:40): anuncia el juicio para mañana. https://x.com/dedreviil/status/1

PIQUES: Westcol vs Gear por el abogado del juicio
CONTINUACIONES: ninguna
MOMENTOS CLIPEADOS: 20:10 · westcol · grita al juez
TEMAS: juicio, abogado, totems
VACÍOS: nada de Otro"""


def _day_with_lore(cfg, db, fecha, contexto="", lore=""):
    import json

    from clipmax.config import session_dir
    s = db.get_or_create_session(fecha)
    if contexto:
        db.update_session(s["id"], x_contexto=contexto)
    if lore:
        folder = session_dir(cfg, fecha) / "claude"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "decision.json").write_text(json.dumps({"lore_para_manana": lore}), encoding="utf-8")
    return s


def test_digest_keeps_grok_sections_and_drops_links():
    from clipmax.xcontext import digest, sections
    secs = sections("Temas del día: un post, no una sección\n\n" + GROK_AYER)
    assert secs["PIQUES"].startswith("Westcol vs Gear") and secs["VACIOS"] == "nada de Otro"
    assert secs["TEMAS"] == "juicio, abogado, totems" and "un post" not in "".join(secs.values())
    d = digest(GROK_AYER)
    assert d.startswith("PIQUES: Westcol vs Gear") and "TEMAS: juicio" in d
    assert "VACÍOS" not in d and "VACIOS" not in d and "https://" not in d
    # Posts sueltos sin secciones: el principio del texto, sin enlaces.
    assert digest("@a: Westcol se pica https://x.com/a/status/9") == "@a: Westcol se pica"
    assert digest("x" * 50, max_chars=10).endswith("[...]")


def test_recurring_topics_ignores_everyday_names():
    from clipmax.xcontext import recurring_topics
    today = [("westcol", 9), ("juicio", 4), ("abogado juicio", 2), ("diamantes", 2)]
    prev = {"2026-09-22": "Westcol pierde los diamantes", "2026-09-23": GROK_AYER}
    out = recurring_topics(today, prev, ignore={"Westcol"})
    assert out == [("juicio", ["2026-09-23"]), ("diamantes", ["2026-09-22"])]


def test_grok_prompt_carries_previous_threads(cfg, db):
    assert "es el primer día" in grok_prompt(cfg, "2026-09-24", db)
    _day_with_lore(cfg, db, "2026-09-18", lore="Demasiado viejo: fuera de los 3 días.")
    for fecha in ("2026-09-19", "2026-09-20"):
        _day_with_lore(cfg, db, fecha, lore=f"Día {fecha}.")
    _day_with_lore(cfg, db, "2026-09-22", lore="Gear juró vengarse de Westcol.")
    db.get_or_create_session("2026-09-21")        # día vacío en medio: no ocupa lugar
    _day_with_lore(cfg, db, "2026-09-23", contexto=GROK_AYER)
    text = grok_prompt(cfg, "2026-09-24", db)
    assert "2026-09-20" in text and "Demasiado viejo" not in text and "2026-09-19" not in text
    assert "{{" not in text and "CONTINUACIONES:" in text
    hilos = text.split("HILOS DE DÍAS ANTERIORES", 1)[1].split("Ignora:", 1)[0]
    # Del más viejo al más reciente; si no hay lore de Claude, se usan los PIQUES de X.
    assert hilos.index("2026-09-22") < hilos.index("2026-09-23")
    assert "Gear juró vengarse" in hilos and "Westcol vs Gear por el abogado" in hilos
    assert "totems" not in hilos
    # Días posteriores no cuentan como historia.
    assert "2026-09-23" not in grok_prompt(cfg, "2026-09-23", db).split("HILOS", 1)[1].split("Ignora:", 1)[0]


def test_day_material_links_today_with_previous_days(cfg, db):
    from clipmax.prompts import build_day_material
    _day_with_lore(cfg, db, "2026-09-23", contexto=GROK_AYER, lore="Westcol y Gear quedaron en juicio.")
    hoy = db.get_or_create_session("2026-09-24")
    material = build_day_material(cfg, db, hoy, [], "@b: hoy es el juicio", [("westcol", 5), ("juicio", 3)])
    assert "## Historia de días anteriores" in material and "### 2026-09-23" in material
    assert "**Lore:** Westcol y Gear quedaron en juicio." in material
    assert "PIQUES: Westcol vs Gear por el abogado" in material
    assert "Temas de hoy que ya venían de días anteriores: juicio (09-23)" in material
    assert "westcol (09-23)" not in material
