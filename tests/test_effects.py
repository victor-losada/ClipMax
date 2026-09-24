"""Subtítulos dinámicos, zoom, efectos de sonido y pantalla dividida."""

import json
import logging
import subprocess
import time

import pytest

from clipmax import brain, editor, effects
from clipmax.recorder import finalize_recordings
from clipmax.transcriber import parse_whisper_json

from .conftest import has_ffmpeg


def test_words_from_whisper_tokens_with_split_accent():
    # whisper puede partir la "á" (0xC3 0xA1) en dos tokens: los bytes se unen antes de decodificar.
    seg = {"offsets": {"from": 0, "to": 1500}, "text": " Gear está aquí", "tokens": [
        {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
        {"text": " Gear", "offsets": {"from": 0, "to": 400}},
        {"text": " est@@1", "offsets": {"from": 400, "to": 700}},
        {"text": "@@2", "offsets": {"from": 700, "to": 800}},
        {"text": " aquí", "offsets": {"from": 800, "to": 1400}},
        {"text": "[_TT_75]", "offsets": {"from": 1500, "to": 1500}},
    ]}
    raw = json.dumps({"transcription": [seg]}, ensure_ascii=False).encode("utf-8")
    raw = raw.replace(b"@@1", b"\xc3").replace(b"@@2", b"\xa1")
    s = parse_whisper_json(raw)[0]
    assert s.text == "Gear está aquí"
    assert [w for *_t, w in s.words] == ["Gear", "está", "aquí"]
    assert s.words[1][:2] == (0.4, 0.8)


def test_incoherent_token_times_fall_back_to_estimate():
    seg = {"offsets": {"from": 0, "to": 2000}, "text": " hola chat",
           "tokens": [{"text": " hola", "offsets": {"from": 0, "to": 0}}, {"text": " chat", "offsets": {"from": 0, "to": 0}}]}
    s = parse_whisper_json(json.dumps({"transcription": [seg]}).encode())[0]
    assert s.words == []
    est = effects.estimate_words(0, 2, "hola chat")
    assert est[0][0] == 0 and est[-1][1] == pytest.approx(2) and est[0][2] == "hola"


def test_remap_skips_cut_silences():
    keep = [(0.0, 4.0), (10.0, 14.0)]
    assert effects.remap(2.0, keep) == 2.0
    assert effects.remap(11.0, keep) == 5.0
    assert effects.remap(6.0, keep) is None
    words = effects.remap_words([(1.0, 1.5, "a"), (6.0, 6.5, "cortada"), (10.5, 11.0, "b")], keep)
    assert [w for *_x, w in words] == ["a", "b"] and words[1][0] == 4.5


def test_segment_words_prefers_quality_transcript():
    segs = [{"fuente": "vivo", "start_ts": 100.0, "end_ts": 102.0, "texto": "hola chat", "palabras": []},
            {"fuente": "candidato", "start_ts": 100.0, "end_ts": 102.0, "texto": "hola chat",
             "palabras": [[100.1, 100.6, "hola"], [100.7, 101.5, "chat"]]}]
    words = effects.segment_words(segs, 100.0, 30.0)
    assert words == [(pytest.approx(0.1), pytest.approx(0.6), "hola"), (pytest.approx(0.7), pytest.approx(1.5), "chat")]


def test_ass_highlights_current_word():
    words = [(0.0, 0.4, "gear"), (0.4, 0.9, "usted"), (0.9, 1.3, "es"), (1.3, 1.9, "llorón")]
    ass = effects.build_ass(words, (1920, 1080), "Arial Black", False, max_words=3)
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 4
    assert "\\c&H18FC53&" in events[1] and "USTED{\\r}" in events[1] and events[1].startswith("Dialogue: 0,0:00:00.40")
    assert "LLORÓN" in events[3] and "GEAR" not in events[3]  # nuevo grupo de palabras
    assert "PlayResX: 1920" in ass and "Arial Black" in ass


def test_zoom_filter_expression():
    z = effects.zoom_filter("vl", "vz", 3.0, (1280, 720), 30, 1.12)
    assert z.startswith("[vl]zoompan=") and "s=1280x720" in z and "it-2.650" in z and z.endswith("[vz]")
    assert "setpts=N/(30*TB)" in z  # sin esto, fps posterior duplica fotogramas sin fin


def _cands():
    return [{"id": 1, "slug": "westcol", "start_ts": 1000.0, "end_ts": 1100.0, "duracion": 100.0, "transcripcion": []},
            {"id": 2, "slug": "gearofnos", "start_ts": 1010.0, "end_ts": 1090.0, "duracion": 80.0, "transcripcion": []},
            {"id": 3, "slug": "westcol", "start_ts": 5000.0, "end_ts": 5060.0, "duracion": 60.0, "transcripcion": []}]


def _clip(**kw):
    base = {"tipo": "clip", "texto": "", "candidato_id": 1, "inicio": 5, "fin": 60, "titulo_en_pantalla": "",
            "prioridad": 5, "motivo": "", "momento_clave": 0, "efecto_sonido": "", "pantalla_dividida_con": 0}
    return {**base, **kw}


def test_validate_effect_fields(cfg):
    raw = {"titulo_video": "T", "guion": [
        _clip(momento_clave=30, efecto_sonido="BOOM", pantalla_dividida_con=2),
        _clip(momento_clave=90, efecto_sonido="no_existe", pantalla_dividida_con=3),
    ]}
    decision, warnings = brain.validate_decision(cfg, raw, _cands())
    a, b = decision["guion"]
    assert (a["momento_clave"], a["efecto_sonido"], a["pantalla_dividida_con"]) == (30, "boom", 2)
    assert (b["momento_clave"], b["efecto_sonido"], b["pantalla_dividida_con"]) == (0, "", 0)
    assert any("desconocido" in w for w in warnings) and any("no es el mismo suceso" in w for w in warnings)


def test_sfx_budget_keeps_claude_picks_first(cfg):
    cfg["edicion"]["sfx_max_por_video"] = 2
    specs = [editor.ClipSpec("a", "A", "x", 0, 10, 0, efecto="boom", whoosh=True),
             editor.ClipSpec("b", "B", "x", 0, 10, 0, efecto="ding", whoosh=True),
             editor.ClipSpec("c", "C", "x", 0, 10, 0, efecto="pop", whoosh=True)]
    editor.budget_sfx(cfg, specs)
    assert [s.efecto for s in specs] == ["boom", "ding", ""]
    assert not any(s.whoosh for s in specs)


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_sfx_library_generates_sounds(cfg):
    lib = effects.sfx_library(cfg)
    assert {"boom", "whoosh", "ding", "pop", "impacto"} <= set(lib)
    assert all(p.stat().st_size > 1000 for p in lib.values())


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_render_with_all_effects(cfg, db, tmp_path, caplog):
    """Render real: subtítulos + zoom + efecto de sonido + pantalla dividida, sin caer al plan B."""
    s = db.get_or_create_session("2026-09-24")
    t0 = time.time() - 200
    for slug, hue in (("westcol", 0), ("gearofnos", 120)):
        src = tmp_path / f"{slug}_parte001.ts"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration=20,hue=h={hue}",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
        p = db.add_part(s["id"], slug, str(src), t0)
        db.close_part(p["id"], t0 + 20, src.stat().st_size)
    finalize_recordings(cfg, db, s)
    db.add_segments(s["id"], "westcol", [
        (t0 + 1, t0 + 4, "Gear usted es un llorón", [[t0 + 1, t0 + 1.6, "Gear"], [t0 + 1.6, t0 + 2.4, "usted"],
                                                      [t0 + 2.4, t0 + 2.8, "es"], [t0 + 2.8, t0 + 3.2, "un"],
                                                      [t0 + 3.2, t0 + 4, "llorón"]]),
        (t0 + 9, t0 + 12, "venga y me lo dice"),
    ], "candidato")
    cands = {1: {"id": 1, "slug": "westcol", "start_ts": t0, "end_ts": t0 + 20, "duracion": 20.0, "transcripcion": []},
             2: {"id": 2, "slug": "gearofnos", "start_ts": t0, "end_ts": t0 + 20, "duracion": 20.0, "transcripcion": []}}
    work = tmp_path / "render"
    work.mkdir()
    lib = effects.sfx_library(cfg)
    size = (640, 360)
    caplog.set_level(logging.WARNING)
    # 1) Clip simple con zoom + boom + subtítulos
    spec = editor.prepare_clip(cfg, db, s, cands[1], 0.0, 14.0, "PRUEBA")
    editor.apply_clip_effects(cfg, db, s, spec, cands[1], _clip(momento_clave=3.0, efecto_sonido="boom"), cands)
    spec.whoosh = True
    assert spec.words and spec.momento == pytest.approx(3.0, abs=0.01)
    out = tmp_path / "zoom.mp4"
    kept = editor.render_clip(cfg, spec, out, size, None, work=work, sfx_lib=lib)
    # 2) Pantalla dividida con el mismo suceso
    spec2 = editor.prepare_clip(cfg, db, s, cands[1], 0.0, 14.0, "")
    editor.apply_clip_effects(cfg, db, s, spec2, cands[1], _clip(pantalla_dividida_con=2), cands)
    assert spec2.partner and spec2.partner.slug == "gearofnos"
    out2 = tmp_path / "split.mp4"
    editor.render_clip(cfg, spec2, out2, size, None, work=work, sfx_lib=lib)
    assert "sin efectos" not in caplog.text
    from clipmax.tools import probe_duration, probe_video_size

    for f in (out, out2):
        assert probe_video_size(f) == size
        assert probe_duration(f) == pytest.approx(kept, abs=0.6)
    assert (work / "zoom.ass").exists() and "USTED" in (work / "zoom.ass").read_text(encoding="utf-8")


def test_whisper_retries_without_ojf(cfg, tmp_path, monkeypatch):
    from clipmax import tools, transcriber

    calls = []

    def fake_run(cmd, timeout=None, **kw):
        calls.append(list(cmd))
        if "-ojf" in cmd:
            raise RuntimeError("Falló whisper-cli (código 1):\nerror: unknown argument: -ojf")
        (tmp_path / "audio.json").write_text('{"transcription": [{"offsets": {"from": 0, "to": 1000}, "text": " hola"}]}')
        return None

    monkeypatch.setattr(tools, "run", fake_run)
    monkeypatch.setattr(tools, "whisper_cli", lambda cfg: "whisper-cli")
    tr = transcriber.WhisperTranscriber(cfg)
    monkeypatch.setattr(tr, "model_path", lambda which: tmp_path / "m.bin")
    segs = tr.transcribe_wav(tmp_path / "audio.wav")
    assert [s.text for s in segs] == ["hola"] and len(calls) == 2 and "-ojf" not in calls[1]
