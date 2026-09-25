"""Director de efectos (estilo Eufonía): emociones, textos que provocan reacciones, facecam."""

import subprocess
import time

import pytest

from clipmax import brain, style
from clipmax.style import Geometry, Mark, build_plan, zoom_to_box

from .conftest import has_ffmpeg

CAM = {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.3}      # cámara arriba a la derecha
CHAT = {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.5}      # chat del stream a la izquierda


def _words(text, t0=0.0, step=0.4):
    return [(t0 + i * step, t0 + i * step + step * 0.9, w) for i, w in enumerate(text.split())]


def test_shouts_are_detected_against_the_clip_itself():
    env = [-40.0] * 100 + [-14.0] * 10 + [-40.0] * 100     # 5 s normal, 0.5 s de grito, 5 s normal
    marks = style.shout_marks(env)
    assert len(marks) == 1 and marks[0].kind == "grito" and marks[0].t == pytest.approx(5.0)
    assert style.shout_marks([-40.0] * 200) == []


def test_lexicon_strong_words_repetitions_and_laughs():
    words = _words("parce me mataron otra vez no no no jajajaja")
    kinds = {(m.kind, m.text) for m in style.lexicon_marks(words, [])}
    assert ("queja", "me mataron") in kinds
    assert ("sorpresa", "no no no") in kinds
    assert ("risa", "jajajaja") in kinds
    # "no" suelto solo cuenta si es gritado.
    quiet = style.lexicon_marks(_words("no sé"), [-40.0] * 40)
    loud = style.lexicon_marks(_words("no sé"), [-40.0] * 2 + [-10.0] * 10 + [-40.0] * 28)
    assert not quiet and loud and loud[0].kind == "sorpresa"


def test_reading_a_chat_message_marks_the_chat_before_the_reaction():
    words = _words("oigan miren esto el man dice que westcol llora demasiado jajaja", t0=10.0)
    chat = [(8.0, "fan123", "westcol llora demasiado"), (9.0, "otro", "hola a todos")]
    marks = style.chat_read_marks(words, chat)
    assert len(marks) == 1 and marks[0].zona == "chat" and marks[0].t == pytest.approx(10.0 + 7 * 0.4)
    assert "fan123" in marks[0].text


def test_cues_point_to_the_right_text_zone():
    words = _words("miren el chat") + _words("mataron a samulx", t0=10.0)
    marks = style.cue_marks(words, has_chat_zone=True)
    assert [(m.zona, round(m.t, 1)) for m in marks] == [("chat", 0.0), ("juego", 9.2)]
    assert [m.zona for m in style.cue_marks(words, has_chat_zone=False)] == ["juego"]


def test_geometry_horizontal_and_vertical():
    h = Geometry((1280, 720), (1920, 1080), "juego_cara", CAM)
    assert h.cam() == pytest.approx((0.75, 0.0, 0.25, 0.3)) and h.cam_anchor() == (1.0, 0.0)
    v = Geometry((1280, 720), (1080, 1920), "juego_cara", CAM)
    x, y, w, hh = v.cam()
    assert (x, y, w) == (0.0, 0.0, 1.0) and hh == pytest.approx(0.38, abs=0.01)
    assert v.cam_anchor() == (0.5, 0.0)
    gx, gy, gw, gh = v.from_source({"x": 0, "y": 0, "w": 1, "h": 1})    # el juego queda abajo, al ancho
    assert gy > hh and gw == 1.0 and gh == pytest.approx(1080 * 720 / 1280 / 1920, abs=0.01)


def test_zoom_to_box_centers_the_text():
    f, ax, ay = zoom_to_box((0.0, 0.0, 0.25, 0.5))
    assert f == pytest.approx(1.7)
    # Ventana visible: [(1-1/f)*ax, +1/f] debe contener el recuadro completo.
    left, top = (1 - 1 / f) * ax, (1 - 1 / f) * ay
    assert left <= 0.0 + 1e-6 and top <= 0.0 + 1e-6 and left + 1 / f >= 0.25 and top + 1 / f >= 0.5


def test_plan_text_zoom_then_reaction_punch_and_facecam():
    geo = Geometry((1280, 720), (1920, 1080), "juego_cara", CAM)
    marks = [Mark(5.0, "texto", 0.8, "chat", zona="chat"), Mark(6.5, "rabia", 0.7, "lexico"),
             Mark(8.0, "queja", 0.6, "lexico"),                      # muy cerca del anterior: no se repite
             Mark(20.0, "susto", 0.95, "claude")]
    words = _words("no puede ser", t0=6.5) + _words("ayuda ayuda", t0=20.0)
    plan = build_plan(marks, 30.0, geo, zona_chat=CHAT, captions_words=words)
    kinds = [(z.kind, round(z.t0, 1)) for z in plan.zooms]
    assert kinds[0] == ("texto", 3.8)                                # el texto, antes de la reacción
    assert ("punch", 6.4) in kinds and not any(k == "punch" and 7.0 < t < 10 for k, t in kinds)
    assert plan.facecam and plan.facecam[0][0] == pytest.approx(19.7)   # la reacción más fuerte, cara completa
    assert plan.facecam_px is not None
    assert [c[2] for c in plan.captions] == ["no puede ser", "ayuda ayuda"]
    # Los punch-ins se anclan en la esquina de la cámara.
    assert all((z.ax, z.ay) == (1.0, 0.0) for z in plan.zooms if z.kind == "punch")


def test_plan_respects_blocks_and_missing_camera():
    geo = Geometry((1280, 720), (1920, 1080), "juego_cara", CAM)
    marks = [Mark(3.0, "grito", 0.9, "voz"), Mark(12.0, "rabia", 0.8, "lexico")]
    words = _words("no no no", t0=3.0)
    pausa = build_plan(marks, 20.0, geo, bloque="pausa", captions_words=words)
    assert not pausa.zooms and not pausa.facecam and pausa.captions == []
    climax = build_plan(marks, 20.0, geo, bloque="climax", captions_words=words)
    assert climax.zooms and climax.captions == []                   # clímax: zoom sí, texto no
    no_cam = build_plan(marks, 20.0, Geometry((1280, 720), (1920, 1080), "juego_cara", None))
    assert len(no_cam.zooms) == 1 and no_cam.zooms[0].factor == pytest.approx(1.12) and not no_cam.facecam
    final = build_plan([], 20.0, geo, zoom_final=True)
    assert final.zooms[-1].kind == "final" and final.zooms[-1].factor == 2.0


def test_validate_decision_style_fields(cfg):
    cands = [{"id": 1, "slug": "westcol", "start_ts": 0.0, "end_ts": 100.0, "duracion": 100.0}]
    narr = [{"tipo": "narracion", "texto": f"n{i}"} for i in range(5)]
    raw = {"titulo_video": "T", "guion": narr[:2] + [
        {"tipo": "gancho", "candidato_id": 1, "inicio": 10, "fin": 20, "momento_clave": 15, "repeticiones": 7},
        {"tipo": "clip", "candidato_id": 1, "inicio": 20, "fin": 60, "bloque": "climax",
         "emociones": [{"t": 30, "tipo": "llanto", "texto": "NOOO"}, {"t": 99, "tipo": "risa", "texto": ""}],
         "zoom_texto": [{"t": 18, "zona": "chat", "texto": "x"}, {"t": 40, "zona": "raro", "texto": ""}],
         "facecam_completo": [{"inicio": 30, "fin": 50}], "rotulo": "ELIMINADO · WESTCOL", "zoom_final": True},
    ] + narr[2:]}
    decision, warnings = brain.validate_decision(cfg, raw, cands)
    g = decision["guion"]
    assert sum(1 for x in g if x["tipo"] == "narracion") == 3 and any("máximo 3" in w for w in warnings)
    hook = next(x for x in g if x["tipo"] == "gancho")
    assert (hook["inicio"], hook["fin"], hook["repeticiones"]) == (13.5, 16.5, 3)
    clip = next(x for x in g if x["tipo"] == "clip")
    assert clip["bloque"] == "climax" and clip["emociones"] == [{"t": 30.0, "tipo": "sorpresa", "texto": "NOOO"}]
    assert [z["zona"] for z in clip["zoom_texto"]] == ["chat", "juego"]
    assert clip["facecam_completo"] == [{"inicio": 30.0, "fin": 38.0}]      # máximo 8 s
    assert clip["rotulo"] == "ELIMINADO · WESTCOL" and clip["zoom_final"] is True


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_eufonia_montage_end_to_end(cfg, db, tmp_path):
    from clipmax import editor
    from clipmax.recorder import finalize_recordings

    cfg["edicion"]["estilo"] = "eufonia"
    for st in cfg["streamers"]:
        if st["slug"] == "westcol":
            st["camara"] = CAM
    s = db.get_or_create_session("2026-09-25")
    t0 = time.time() - 300
    src = tmp_path / "westcol_parte001.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=40",
                    "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=40",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    p = db.add_part(s["id"], "westcol", str(src), t0)
    db.close_part(p["id"], t0 + 40, src.stat().st_size)
    finalize_recordings(cfg, db, s)
    db.add_segments(s["id"], "westcol", [(t0 + 5, t0 + 7, "no puede ser me mataron",
                                          [[t0 + 5, t0 + 5.4, "no"], [t0 + 5.4, t0 + 5.9, "puede"],
                                           [t0 + 5.9, t0 + 6.3, "ser"], [t0 + 6.3, t0 + 6.6, "me"],
                                           [t0 + 6.6, t0 + 7.0, "mataron"]])], "candidato")
    cands = [{"id": 1, "slug": "westcol", "nombre": "Westcol", "start_ts": t0, "end_ts": t0 + 40, "duracion": 40.0,
              "transcripcion": [[5.0, 7.0, "no puede ser me mataron"]]}]
    raw = {"titulo_video": "El juicio", "guion": [
        {"tipo": "gancho", "candidato_id": 1, "inicio": 5.0, "fin": 7.0, "repeticiones": 2},
        {"tipo": "narracion", "texto": "Todo empezó con una mina."},
        {"tipo": "clip", "candidato_id": 1, "inicio": 2.0, "fin": 14.0, "bloque": "cuerpo",
         "emociones": [{"t": 6.3, "tipo": "queja", "texto": "me mataron"}], "rotulo": "ELIMINADO · WESTCOL"}]}
    decision, _w = brain.validate_decision(cfg, raw, cands)
    out = editor.render_summary(cfg, db, s, decision, cands)
    from clipmax.tools import probe_duration, probe_video_size
    assert probe_video_size(out) == (640, 360)
    # 2 repeticiones del gancho (2 s) + sting (4 s) + narración (3.1 s) + clip + pantalla final (4.5 s).
    # Del clip (12 s) solo queda la voz (3-5 s) con márgenes y la reacción: 4.2 s (silencios > 1.5 s fuera).
    assert probe_duration(out) == pytest.approx(2 * 2 + 4 + 3.1 + 4.2 + 4.5, abs=0.8)
    assert db.outputs(s["id"])[0]["meta"]["estilo"] == "eufonia"


def test_hook_is_tightened_to_the_strongest_words(monkeypatch):
    from clipmax import montage
    from clipmax.editor import ClipSpec

    spec = ClipSpec("westcol", "Westcol", "x.mp4", 0.0, 6.0, 0.0, keep=[(0.0, 6.0)],
                    words=_words("bueno entonces yo le dije que me mataron otra vez"))
    monkeypatch.setattr(style, "loudness", lambda *a: [-40.0] * 120)
    montage.tighten_hook(spec)
    (a, b), = spec.keep
    assert a == pytest.approx(6 * 0.4 - 0.15) and b - a <= 2.9 and b > 6 * 0.4 + 0.4   # "me mataron..."
