import subprocess
import time

import pytest

from clipmax import editor
from clipmax.recorder import finalize_recordings, locate_range

from .conftest import has_ffmpeg


def test_speech_keep_intervals_cuts_long_silences():
    speech = [(1.0, 4.0), (5.0, 8.0), (20.0, 25.0)]
    keep = editor.speech_keep_intervals(40.0, speech, max_gap=2.5, pad=0.3, tail=1.5)
    assert keep == [(0.7, 8.3), (19.7, 26.8)]
    assert editor.speech_keep_intervals(10.0, [], 2.5) == [(0.0, 10.0)]
    # Casi sin voz (momento visual): no se recorta a un segundo, se deja completo.
    assert editor.speech_keep_intervals(42.0, [(41.5, 42.0)], 2.5) == [(0.0, 42.0)]


def test_snap_to_segments():
    segs = [[4.0, 9.0, "frase"], [12.0, 15.0, "otra"]]
    a, b = editor.snap_to_segments(5.0, 13.0, segs, 60)
    assert a == pytest.approx(3.75) and b == pytest.approx(15.3)
    assert editor.snap_to_segments(0.5, 30, segs, 20) == (0.5, 20)


def test_layout_variants():
    h = editor.layout_filter("vc", "vl", (1920, 1080), (1920, 1080), "juego_cara", None)
    assert h == "[vc]scale=1920:1080:flags=bicubic,setsar=1[vl]"
    cam = {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.3}
    face = editor.layout_filter("vc", "vl", (1920, 1080), (1920, 1080), "cara", cam)
    assert face.startswith("[vc]crop=480:324:1440:0[lcam]") and "boxblur" in face
    vert = editor.layout_filter("vc", "vl", (1920, 1080), (1080, 1920), "juego_cara", cam)
    assert "vstack=inputs=2[vl]" in vert and "crop=1080:728" in vert


def test_split_layout_structure():
    cam = {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.3}
    g = editor.split_layout("vc", "vcp", "vl", ((1920, 1080), {"camara": cam}), ((1920, 1080), {"camara": None}),
                            (1920, 1080))
    assert "[vc]crop=480:324:1440:0" in g and "hstack=inputs=2" in g and g.endswith("[vl]")
    v = editor.split_layout("vc", "vcp", "vl", ((1920, 1080), {}), ((1920, 1080), {}), (1080, 1920))
    assert "vstack=inputs=2" in v


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_render_pipeline_with_real_ffmpeg(cfg, db, tmp_path):
    """Integración: parte .ts -> MP4 final -> clip con silencios recortados + tarjeta -> concat."""
    s = db.get_or_create_session("2026-09-23")
    src = tmp_path / "westcol_parte001.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=20",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    t0 = time.time() - 100
    part = db.add_part(s["id"], "westcol", str(src), t0)
    db.close_part(part["id"], t0 + 20, src.stat().st_size)
    finalize_recordings(cfg, db, s)
    parts = db.list_parts(s["id"], "westcol")
    assert parts[0]["final_path"].endswith("2026-09-23_westcol.mp4") and not src.exists()
    loc = locate_range(db, s["id"], "westcol", t0 - 5, t0 + 12)
    assert loc and loc[1] == 0.0 and loc[2] == pytest.approx(12, abs=0.1) and loc[3] == pytest.approx(t0)

    # Voz en 1-4 s y 10-14 s: el hueco de 6 s se recorta.
    db.add_segments(s["id"], "westcol", [(t0 + 1, t0 + 4, "Gear venga"), (t0 + 10, t0 + 14, "y responda")], "candidato")
    cand = {"id": 1, "slug": "westcol", "start_ts": t0, "end_ts": t0 + 20, "duracion": 20.0, "transcripcion": []}
    spec = editor.prepare_clip(cfg, db, s, cand, 0.0, 16.0, "PRUEBA")
    assert len(spec.keep) == 2 and spec.kept_duration < 12
    size = (640, 360)
    title = editor.render_lower_third("PRUEBA", "Westcol", size, tmp_path / "t.png")
    clip = tmp_path / "clip.mp4"
    kept = editor.render_clip(cfg, spec, clip, size, title)
    card = tmp_path / "card.mp4"
    card_dur = editor.render_card_piece(cfg, "Narración de prueba", card, size, voice=False)
    final = editor.concat_pieces([card, clip], tmp_path / "final.mp4")
    from clipmax.tools import probe_duration, probe_video_size

    assert probe_video_size(final) == size
    assert probe_duration(final) == pytest.approx(kept + card_dur, abs=0.6)
