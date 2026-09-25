"""Resumen TikTok narrado (ficha vertical): planos, subtítulos, contadores, narrador y render."""

import array
import subprocess
import sys
import time
import types
import wave

import pytest
from PIL import Image

from clipmax import brain, editor, narrator, tiktok_recap
from clipmax.recorder import finalize_recordings
from clipmax.tiktok_recap import CLIP_Y, SUB_Y, W, build_recap_ass, counters_png, fit_to_limit, plan_shots

from .conftest import has_ffmpeg

COUNTERS = [{"id": "muertes", "etiqueta": "MUERTES", "icono": "calavera", "inicial": 3},
            {"id": "aliados", "etiqueta": "ALIADOS", "icono": "corazon", "inicial": 1}]


def _covered(shots):
    return sum(s.b - s.a for s in shots)


def test_shots_cover_the_narration_with_fast_cuts():
    shots = plan_shots(9.0, 10.0, 60.0, 40.0, None)
    assert _covered(shots) == pytest.approx(9.0)
    assert all(10.0 <= s.a < s.b <= 60.0 for s in shots)
    assert max(s.b - s.a for s in shots) <= 2.6 and len(shots) >= 4
    assert [s.a for s in shots] == sorted(s.a for s in shots)          # en orden, sin saltos atrás
    assert shots[-1].b == pytest.approx(41.5)                           # termina justo después del momento clave
    assert shots[0].a >= 40.0 - 3 * 9.0 - 1                             # material cercano al momento clave


def test_burst_puts_the_impact_frame_on_the_keyword():
    shots = plan_shots(5.0, 20.0, 60.0, 40.0, burst_at=2.6)
    assert _covered(shots) == pytest.approx(5.0)
    t, impact = 0.0, None
    for s in shots:
        if s.impact:
            impact = (t, s.a)
        t += s.b - s.a
    assert impact == (pytest.approx(2.6), pytest.approx(40.0))
    micro = [s for s in shots if s.b - s.a <= 0.35]
    assert len(micro) >= 4                                               # ráfaga de micro-cortes


def test_short_material_repeats_instead_of_failing():
    shots = plan_shots(6.0, 0.0, 5.0, None, None)
    assert _covered(shots) == pytest.approx(6.0) and all(0 <= s.a < s.b <= 5 for s in shots)


def test_fit_to_limit_drops_low_priority_first(cfg):
    cfg["edicion"]["resumen_tiktok_max_s"] = 30
    events = [{"prioridad": 5}, {"prioridad": 1}, {"prioridad": 3}, {"prioridad": 1}]
    assert fit_to_limit(cfg, events, [10, 10, 10, 10], fixed=5) == [0, 2]
    assert fit_to_limit(cfg, events, [5, 5, 5, 5], fixed=5) == [0, 1, 2, 3]


def test_recap_subtitles_highlight_names_numbers_and_current_word(cfg):
    words = [(0.0, 0.3, "Westcol"), (0.3, 0.6, "murió"), (0.6, 0.9, "tres"), (0.9, 1.2, "veces.")]
    ass = build_recap_ass(words, tiktok_recap.highlight_set(cfg), "Titan One")
    assert f",8,{int(W * 0.05)},{int(W * 0.05)},{SUB_Y}," in ass            # debajo del clip, centrado
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert len(lines) == 4                                                  # una línea por palabra dicha
    yellow = "\\c&H0000D4FF&"
    # En la palabra "murió", WESTCOL sigue amarilla (nombre) y MURIÓ es la actual (amarilla y grande).
    assert f"{{{yellow}}}WESTCOL" in lines[1] and f"{yellow}\\fscx112" in lines[1] and "MURIÓ" in lines[1]
    assert "TRES" not in lines[1]                                           # bloques de máx. 16 letras
    assert f"{{{yellow}}}TRES" in lines[3] and lines[3].count("fscx112") == 1   # las cifras también


def test_counters_sit_on_the_clip_corners(tmp_path):
    out = counters_png(COUNTERS, {"muertes": 4, "aliados": 0}, tmp_path / "c.png", highlight="muertes")
    img = Image.open(out)
    assert img.size == (1080, 1920)
    bbox = img.getbbox()
    assert bbox[1] >= CLIP_Y and bbox[3] < CLIP_Y + 300                     # esquinas superiores del clip
    left = img.crop((0, CLIP_Y, 400, CLIP_Y + 300)).getbbox()
    right = img.crop((680, CLIP_Y, 1080, CLIP_Y + 300)).getbbox()
    assert left and right
    big = Image.open(counters_png(COUNTERS, {}, tmp_path / "g.png", big=True))
    assert big.getbbox()[2] - big.getbbox()[0] > 500                        # intro: contadores grandes


def test_validate_narrated_tiktok_fields(cfg):
    cands = [{"id": 1, "slug": "westcol", "start_ts": 0.0, "end_ts": 100.0, "duracion": 100.0}]
    raw = {"titulo_video": "T", "guion": [{"tipo": "clip", "candidato_id": 1, "inicio": 0, "fin": 60}],
           "tiktok_contadores": [{"id": "Muertes!", "etiqueta": "muertes", "icono": "calavera", "inicial": 2},
                                 {"id": "muertes", "etiqueta": "repetido", "icono": "x"},
                                 {"id": "alianzas", "etiqueta": "alianzas", "icono": "raro"}],
           "tiktok_intro": "Día 4: 3 muertes.", "tiktok_cierre": "Sígueme.",
           "resumen_tiktok": [
               {"candidato_id": 1, "inicio": 10, "fin": 40, "momento_clave": 30, "narracion": "Westcol murió otra vez.",
                "tipo_evento": "MUERTE", "contador": "muertes", "suma": 9, "palabra_clave": "murió",
                "cita_inicio": 31, "cita_fin": 34, "prioridad": 7},
               {"candidato_id": 1, "inicio": 40, "fin": 70, "narracion": "Y luego se fue.", "tipo_evento": "raro",
                "contador": "nada", "suma": 1, "palabra_clave": "explotó", "cita_inicio": 50, "cita_fin": 59}]}
    decision, _w = brain.validate_decision(cfg, raw, cands)
    assert [c["id"] for c in decision["tiktok_contadores"]] == ["muertes", "alianzas"]
    assert decision["tiktok_contadores"][1]["icono"] == "estrella"
    a, b = decision["resumen_tiktok"]
    assert (a["tipo_evento"], a["contador"], a["suma"], a["palabra_clave"], a["prioridad"]) == \
        ("muerte", "muertes", 5, "murió", 5)
    assert (a["cita_inicio"], a["cita_fin"]) == (31.0, 34.0)
    assert (b["tipo_evento"], b["contador"], b["suma"], b["palabra_clave"]) == ("otro", "", 0, "")
    assert (b["cita_inicio"], b["cita_fin"]) == (0.0, 0.0)                  # cita de 9 s: demasiado larga
    assert decision["tiktok_intro"] == "Día 4: 3 muertes." and decision["tiktok_cierre"] == "Sígueme."


# ---------------------------------------------------------------------------
# Narrador (Piper simulado: la prueba no depende de la voz descargada)
# ---------------------------------------------------------------------------
class _FakeVoice:
    config = types.SimpleNamespace(sample_rate=16000)

    def synthesize(self, text, syn_config=None):
        # silencio - voz (0.06 s por letra) - silencio: el narrador debe recortar las puntas
        n = int(0.06 * len(text) * 16000)
        pcm = array.array("h", [0] * 3200 + [4000] * n + [0] * 3200)
        yield types.SimpleNamespace(audio_int16_bytes=pcm.tobytes())


@pytest.fixture
def fake_piper(monkeypatch):
    mod = types.ModuleType("piper")
    mod.SynthesisConfig = lambda **kw: kw
    mod.PiperVoice = types.SimpleNamespace(load=lambda p: _FakeVoice())
    monkeypatch.setitem(sys.modules, "piper", mod)
    narrator._voice.cache_clear()
    yield
    narrator._voice.cache_clear()


def test_narrator_has_no_long_pauses_and_times_every_word(cfg, fake_piper, tmp_path):
    for st in cfg["streamers"]:
        if st["slug"] == "westcol":
            st["pronunciacion"] = "Güéstcol"
    nar = narrator.narrate(cfg, "Westcol llegó, vio la base. Y murió.", tmp_path / "n.wav")
    assert [w for _a, _b, w in nar.words] == ["Westcol", "llegó,", "vio", "la", "base.", "Y", "murió."]
    gaps = [nar.words[i + 1][0] - nar.words[i][1] for i in range(len(nar.words) - 1)]
    assert max(gaps) <= 0.3 and all(g >= -1e-6 for g in gaps)
    with wave.open(str(tmp_path / "n.wav")) as wf:
        assert wf.getframerate() == 16000 and wf.getnframes() / 16000 == pytest.approx(nar.dur)
    # Se recortan los 0.2 s de silencio de cada punta: 3 frases -> casi sin silencio.
    assert nar.dur < 0.06 * len("Güéstcol llegó,vio la base.Y murió.") + 0.5
    assert narrator._speakable(cfg, "westcol ganó") == "Güéstcol ganó"


def test_narrator_availability_needs_the_voice(cfg, fake_piper, tmp_path):
    cfg["edicion"]["piper_voz"] = str(tmp_path / "no.onnx")
    assert not narrator.available(cfg)
    (tmp_path / "no.onnx").write_bytes(b"x")
    assert narrator.available(cfg)


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_render_narrated_recap_end_to_end(cfg, db, tmp_path, fake_piper):
    s = db.get_or_create_session("2026-09-25")
    t0 = time.time() - 300
    src = tmp_path / "westcol_parte001.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60:duration=40",
                    "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=40",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    p = db.add_part(s["id"], "westcol", str(src), t0)
    db.close_part(p["id"], t0 + 40, src.stat().st_size)
    finalize_recordings(cfg, db, s)
    for st in cfg["streamers"]:
        if st["slug"] == "westcol":
            st["camara"] = {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.3}
    cfg["edicion"]["piper_voz"] = str(tmp_path / "voz.onnx")
    (tmp_path / "voz.onnx").write_bytes(b"x")
    cands = [{"id": 1, "slug": "westcol", "nombre": "Westcol", "start_ts": t0, "end_ts": t0 + 40, "duracion": 40.0,
              "transcripcion": []}]
    raw = {"titulo_video": "T", "guion": [{"tipo": "clip", "candidato_id": 1, "inicio": 0, "fin": 30}],
           "tiktok_contadores": COUNTERS, "tiktok_intro": "Día cuatro: 3 muertes.", "tiktok_cierre": "Sígueme.",
           "resumen_tiktok": [
               {"candidato_id": 1, "inicio": 2, "fin": 20, "momento_clave": 12, "narracion": "Westcol murió en la mina.",
                "tipo_evento": "muerte", "contador": "muertes", "suma": 1, "palabra_clave": "murió",
                "cita_inicio": 14, "cita_fin": 16.5, "prioridad": 5},
               {"candidato_id": 1, "inicio": 20, "fin": 38, "narracion": "Luego hizo una alianza.",
                "tipo_evento": "alianza", "contador": "aliados", "suma": 1, "palabra_clave": "alianza",
                "prioridad": 3}]}
    decision, _w = brain.validate_decision(cfg, raw, cands)
    out = editor.render_tiktok_summary(cfg, db, s, decision, cands)
    from clipmax.tools import probe_duration, probe_video_size
    assert out and out.name == "resumen_tiktok_2026-09-25.mp4"
    assert probe_video_size(out) == (1080, 1920)
    # La voz simulada habla 0.06 s por letra: intro 1.5 + 0.35 s, hecho 1 (1.5 s + 2.5 s de cita),
    # hecho 2 (1.4 s) y cierre (mínimo 3.5 s).
    assert probe_duration(out) == pytest.approx(10.8, abs=1.2)
    meta = db.outputs(s["id"])[0]["meta"]
    assert meta["narrado"] is True
    assert not (out.parent / "render_tiktok").exists()
