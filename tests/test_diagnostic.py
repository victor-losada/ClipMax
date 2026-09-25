"""Diagnóstico de sincronía: mide el desfase audio/video que agrega la edición."""

import subprocess

import pytest

from clipmax import diagnostic

from .conftest import has_ffmpeg


def _source(tmp_path):
    """60 fps con destellos y pitidos a intervalos irregulares (lo periódico sería ambiguo)."""
    src = tmp_path / "grabacion.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=60:duration=70,drawbox=x=40:y=30:w=120:h=90"
                    ":color=white:t=fill:enable='lt(mod(t*1.3+sin(t*0.7),1),0.2)'",
                    "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t*1.7+sin(t*1.1),1),0.15),0.7*sin(2*PI*440*t),0)'"
                    ":s=48000:d=70",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "120", "-c:a", "aac", str(src)], check=True)
    return src


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_measures_render_offset(cfg, tmp_path):
    src = _source(tmp_path)
    ok = diagnostic.measure_render(cfg, str(src), 20.37)
    assert abs(ok["desfase_s"]) <= 0.05 and ok["confianza_audio"] > 0.5
    cfg["edicion"]["desfase_audio_s"] = 0.5       # la voz 0.5 s tarde
    late = diagnostic.measure_render(cfg, str(src), 20.37)
    assert late["desfase_s"] == pytest.approx(-0.5, abs=0.05)


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_report_suggests_setting(cfg, db, tmp_path):
    src = _source(tmp_path)
    s = db.get_or_create_session("2026-09-24")
    p = db.add_part(s["id"], "westcol", str(tmp_path / "x.ts"), 1000.0)
    db.update_part(p["id"], final_path=str(src), offset_in_final=0.0)
    text = diagnostic.run(cfg, db, "2026-09-24")
    assert "conserva la sincronía" in text and "60.00 fps" in text
    cfg["edicion"]["desfase_audio_s"] = -0.3      # la voz 0.3 s antes
    text = diagnostic.run(cfg, db, "2026-09-24")
    import re
    m = re.search(r"voz ([\d.]+)s antes .*«Desfase del audio» = (-?[\d.]+)", text)
    assert m, text
    # La medición del video tiene resolución de 1 fotograma (1/30 s).
    assert float(m.group(1)) == pytest.approx(0.3, abs=0.04) and abs(float(m.group(2))) <= 0.04
