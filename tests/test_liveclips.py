"""Clips para TikTok armados mientras se graba."""

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from clipmax import brain, liveclips
from clipmax.liveclips import LiveClipper, fit_window, normalize_decision

from .conftest import has_ffmpeg


def _session_with_moment(cfg, db, tmp_path, n_moments=1):
    """Una parte grabada de 60 s que terminó hace 2 minutos y un momento detectado dentro."""
    s = db.get_or_create_session("2026-09-25")
    src = tmp_path / "westcol_parte001.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=60",
                    "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=60",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    t0 = time.time() - 180
    p = db.add_part(s["id"], "westcol", str(src), t0)
    db.close_part(p["id"], t0 + 60, src.stat().st_size)
    moments = [{"slug": "westcol", "start_ts": t0 + 5 + 25 * i, "end_ts": t0 + 45 + 25 * i, "score": 5.0 - i,
                "rank": i + 1, "componentes": {"senales": {"pico_chat": 1}, "pareja": True, "con": ["gearofnos"]}}
               for i in range(n_moments)]
    db.replace_moments(s["id"], moments)
    return s


def _fake_curate(result):
    calls = []

    def fake(cfg, db, session, material):
        calls.append(material)
        return dict(result)
    return fake, calls


GOOD = {"publicar": True, "motivo": "Gear se pica", "inicio": 5, "fin": 23, "momento_clave": 15,
        "titulo": "GEAR NO AGUANTÓ", "caption": "Westcol lo dijo en vivo y Gear respondió", "hashtags": ["dedsafio", "#kick"],
        "efecto_sonido": "boom"}


def test_fit_window_respects_limits_and_keeps_punchline(cfg):
    cfg["clips_vivo"].update(duracion_min_s=15, duracion_max_s=60)
    assert fit_window(cfg, 10, 150, 160, key=140) == (86.0, 146.0)   # muy largo: se recorta sin perder el remate
    a, b = fit_window(cfg, 50, 55, 160, key=52)                        # muy corto: se amplía
    assert b - a == pytest.approx(15) and a <= 52 <= b
    assert fit_window(cfg, 30, 10, 40, key=None) == (0.0, 40.0)        # al revés: todo el tramo permitido


def test_normalize_decision_cleans_claude_output(cfg):
    cand = {"slug": "westcol", "nombre": "Westcol", "duracion": 70.0}
    d = normalize_decision(cfg, {**GOOD, "momento_clave": 500, "efecto_sonido": "no-existe",
                                 "hashtags": ["Desafío 4", "#kick", "#kick"]}, cand)
    assert d["momento_clave"] is None and d["efecto_sonido"] == ""
    assert d["hashtags"][:2] == ["#Desafío4", "#kick"] and "#westcol" in d["hashtags"]
    assert d["fin"] - d["inicio"] >= cfg["clips_vivo"]["duracion_min_s"]


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_live_clip_with_claude_then_no_repeat(cfg, db, tmp_path, monkeypatch):
    cfg["claude"]["modo"] = "api"
    s = _session_with_moment(cfg, db, tmp_path)
    fake, calls = _fake_curate(GOOD)
    monkeypatch.setattr(brain, "curate_live_clip", fake)
    clipper = LiveClipper(cfg, db, s)
    cid = clipper.step()
    c = db.live_clip(cid)
    assert c["estado"] == "listo", c["nota"]
    assert c["origen"] == "claude" and c["titulo"] == "GEAR NO AGUANTÓ" and "#dedsafio" in c["hashtags"]
    from clipmax.tools import probe_duration, probe_video_size
    assert probe_video_size(c["path"]) == (1080, 1920)
    assert probe_duration(c["path"]) == pytest.approx(18, abs=1.5)
    assert c["thumb"] and "clips_vivo" in c["path"]
    assert "Por qué se detectó" in calls[0] and "Westcol" in calls[0]
    # El mismo momento no se vuelve a clipear.
    assert clipper.step() is None and len(calls) == 1


def test_only_hard_reasons_discard_a_clip(cfg):
    base = {"publicar": False, "descarte": "", "motivo": "banter sin remate"}
    assert liveclips.should_publish(cfg, {**base, "publicar": True}) == (True, "banter sin remate")
    ok, nota = liveclips.should_publish(cfg, {**base, "descarte": "poco_interes"})
    assert ok and "flojo" in nota                                            # lo flojo se publica igual
    for hard in ("sin_contenido", "tecnico", "publicidad"):
        assert liveclips.should_publish(cfg, {**base, "descarte": hard}) == (False, "banter sin remate")
    assert liveclips.should_publish(cfg, {**base, "descarte": "tecnico"}, force=True)[0]   # pedido a mano
    cfg["clips_vivo"]["descartar_flojos"] = True
    assert not liveclips.should_publish(cfg, {**base, "descarte": "poco_interes"})[0]
    # Claude dijo que no sin categoría: cuenta como "flojo".
    cand = {"slug": "westcol", "nombre": "Westcol", "duracion": 70.0}
    d = normalize_decision(cfg, {**GOOD, "publicar": False, "inicio": 0, "fin": 0, "titulo": ""}, cand)
    assert d["descarte"] == "poco_interes" and d["sin_corte"] and d["sin_titulo"]


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_live_clip_discarded_by_claude_then_published_anyway(cfg, db, tmp_path, monkeypatch):
    cfg["claude"]["modo"] = "api"
    s = _session_with_moment(cfg, db, tmp_path)
    monkeypatch.setattr(brain, "curate_live_clip", _fake_curate({
        **GOOD, "publicar": False, "descarte": "sin_contenido", "motivo": "pantalla de espera",
        "inicio": 0, "fin": 0, "titulo": ""})[0])
    clipper = LiveClipper(cfg, db, s)
    cid = clipper.step()
    c = db.live_clip(cid)
    assert c["estado"] == "descartado" and c["nota"] == "pantalla de espera" and not c["path"]
    # Botón "Publicar igual": mismo clip, corte y título de las reglas automáticas.
    clipper.force(cid)
    assert clipper.step() == cid
    c = db.live_clip(cid)
    assert c["estado"] == "listo" and c["path"] and "publicado a mano" in c["nota"]
    assert c["titulo"] and len(db.live_clips(s["id"])) == 1


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_weak_moment_is_published_by_default(cfg, db, tmp_path, monkeypatch):
    cfg["claude"]["modo"] = "api"
    s = _session_with_moment(cfg, db, tmp_path)
    monkeypatch.setattr(brain, "curate_live_clip", _fake_curate({
        **GOOD, "publicar": False, "descarte": "poco_interes", "motivo": "transcripción fragmentaria"})[0])
    c = db.live_clip(LiveClipper(cfg, db, s).step())
    assert c["estado"] == "listo" and "flojo" in c["nota"] and c["titulo"] == "GEAR NO AGUANTÓ"


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_live_clip_without_claude_and_hourly_cap(cfg, db, tmp_path, monkeypatch):
    cfg["claude"]["modo"] = "manual"          # sin API: reglas automáticas
    cfg["clips_vivo"]["max_por_hora"] = 1
    s = _session_with_moment(cfg, db, tmp_path, n_moments=2)
    monkeypatch.setattr(brain, "curate_live_clip", lambda *a: pytest.fail("no debe llamar a Claude"))
    clipper = LiveClipper(cfg, db, s)
    c = db.live_clip(clipper.step())
    assert c["estado"] == "listo" and c["origen"] == "auto" and c["titulo"]
    assert clipper.step() is None and "tope" in clipper.status["detalle"]
    # Pedido a mano desde el Panel: salta el tope por hora.
    second = [m for m in db.moments(s["id"]) if m["rank"] == 2][0]
    clipper.request(second["id"])
    c2 = db.live_clip(clipper.step())
    assert c2["estado"] == "listo" and c2["start_ts"] == second["start_ts"]


def test_curate_live_clip_request(cfg, db, monkeypatch):
    seen = {}

    class FakeClient:
        class messages:
            @staticmethod
            def count_tokens(**_):
                return SimpleNamespace(input_tokens=1500)

            @staticmethod
            def create(**params):
                seen.update(params)
                usage = SimpleNamespace(input_tokens=1500, output_tokens=200, cache_creation_input_tokens=0,
                                        cache_read_input_tokens=0, server_tool_use=None)
                return SimpleNamespace(model=params["model"], stop_reason="end_turn", usage=usage,
                                       content=[SimpleNamespace(type="text", text=json.dumps(GOOD))])

    monkeypatch.setattr(brain, "_client", lambda: FakeClient())
    out = brain.curate_live_clip(cfg, db, {"id": 1, "fecha": "2026-09-25"}, "material")
    assert out["titulo"] == "GEAR NO AGUANTÓ"
    assert seen["model"] == "claude-haiku-4-5" and "thinking" not in seen
    assert seen["output_config"] == {"format": {"type": "json_schema", "schema": liveclips.brain.CLIP_SCHEMA}}
    assert "TikTok" in seen["system"] and "{{" not in seen["system"]
    run = db.claude_runs()[0]
    assert run["tipo"] == "clip_vivo" and run["costo_usd"] == pytest.approx(1500 * 1 / 1e6 + 200 * 5 / 1e6)
    # La API rechaza el esquema (gramática muy grande): se repite sin él y se lee el JSON del texto.
    import anthropic
    import httpx2

    calls = []

    def create(**params):
        calls.append(dict(params))
        if "format" in params.get("output_config", {}):
            resp = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
            raise anthropic.BadRequestError("The compiled grammar is too large", response=resp, body=None)
        usage = SimpleNamespace(input_tokens=1500, output_tokens=200, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, server_tool_use=None)
        return SimpleNamespace(model=params["model"], stop_reason="end_turn", usage=usage,
                               content=[SimpleNamespace(type="text", text="```json\n" + json.dumps(GOOD) + "\n```")])
    FakeClient.messages.create = staticmethod(create)
    brain._SCHEMA_OFF.discard("clip")
    out = brain.curate_live_clip(cfg, db, {"id": 1, "fecha": "2026-09-25"}, "material")
    assert out["titulo"] == "GEAR NO AGUANTÓ" and len(calls) == 2 and "output_config" not in calls[1]
    brain.curate_live_clip(cfg, db, {"id": 1, "fecha": "2026-09-25"}, "material")
    assert len(calls) == 3 and "output_config" not in calls[2]            # ya no se reintenta con esquema
    brain._SCHEMA_OFF.discard("clip")
    # Presupuesto agotado: no se llama.
    cfg["claude"]["presupuesto_mensual_usd"] = 0.0001
    with pytest.raises(brain.BudgetExceeded):
        brain.curate_live_clip(cfg, db, {"id": 1, "fecha": "2026-09-25"}, "material")
