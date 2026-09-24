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
