"""Resumen vertical para TikTok (máximo 4 minutos) al cierre del día."""

import subprocess
import time

import pytest

from clipmax import brain, editor
from clipmax.recorder import finalize_recordings

from .conftest import has_ffmpeg

CANDS = [
    {"id": 1, "slug": "westcol", "nombre": "Westcol", "start_ts": 1000.0, "end_ts": 1100.0, "duracion": 100.0,
     "transcripcion": []},
    {"id": 2, "slug": "gearofnos", "nombre": "Gear of Nos", "start_ts": 2000.0, "end_ts": 2090.0, "duracion": 90.0,
     "transcripcion": []},
    {"id": 3, "slug": "westcol", "nombre": "Westcol", "start_ts": 500.0, "end_ts": 560.0, "duracion": 60.0,
     "transcripcion": []},
]


def _raw(**over):
    raw = {"titulo_video": "T", "guion": [
        {"tipo": "clip", "candidato_id": 1, "inicio": 0, "fin": 90, "prioridad": 5, "momento_clave": 60},
        {"tipo": "clip", "candidato_id": 2, "inicio": 0, "fin": 80, "prioridad": 3, "momento_clave": 0},
        {"tipo": "clip", "candidato_id": 3, "inicio": 0, "fin": 50, "prioridad": 2, "momento_clave": 20}],
        "mejores_momentos": [
            {"candidato_id": 3, "inicio": 0, "fin": 50, "titulo": "EL PRIMERO"},
            {"candidato_id": 1, "inicio": 10, "fin": 90, "titulo": "EL GANCHO"},
            {"candidato_id": 2, "inicio": 0, "fin": 80, "titulo": "EL ÚLTIMO"}]}
    raw.update(over)
    return raw


def test_validate_caps_tiktok_summary(cfg):
    cfg["edicion"]["resumen_tiktok_max_s"] = 60
    raw = _raw(resumen_tiktok=[
        {"candidato_id": 1, "inicio": 0, "fin": 100, "texto_en_pantalla": "LARGO", "momento_clave": 70},
        {"candidato_id": 99, "inicio": 0, "fin": 10, "texto_en_pantalla": "NO EXISTE", "momento_clave": 0},
        {"candidato_id": 2, "inicio": 5, "fin": 35, "texto_en_pantalla": "SE RECORTA", "momento_clave": 0},
        {"candidato_id": 3, "inicio": 0, "fin": 20, "texto_en_pantalla": "YA NO CABE", "momento_clave": 0}],
        caption_resumen_tiktok="El día en 1 minuto #Desafio4")
    decision, warnings = brain.validate_decision(cfg, raw, CANDS)
    tk = decision["resumen_tiktok"]
    # El tramo largo se deja en 45 s terminando poco después del remate (70 + 5).
    assert (tk[0]["inicio"], tk[0]["fin"], tk[0]["momento_clave"]) == (30.0, 75.0, 70.0)
    assert (tk[1]["inicio"], tk[1]["fin"]) == (5.0, 20.0)           # recortado para no pasar de 60 s
    assert len(tk) == 2 and sum(t["fin"] - t["inicio"] for t in tk) == pytest.approx(60)
    assert decision["caption_resumen_tiktok"].startswith("El día")
    assert any("99" in w for w in warnings)


def test_fallback_items_hook_first_then_chronological(cfg):
    decision, _ = brain.validate_decision(cfg, _raw(), CANDS)
    items = editor.tiktok_summary_items(cfg, decision, CANDS)
    assert [i["texto_en_pantalla"] for i in items] == ["EL GANCHO", "EL PRIMERO", "EL ÚLTIMO"]
    # termina 5 s después del remate (60) y dura como mucho 30 s
    assert (items[0]["inicio"], items[0]["fin"]) == (35.0, 65.0)


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_render_tiktok_summary(cfg, db, tmp_path):
    s = db.get_or_create_session("2026-09-25")
    t0 = time.time() - 300
    for slug in ("westcol", "gearofnos"):
        src = tmp_path / f"{slug}_parte001.ts"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=40",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=40",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
        p = db.add_part(s["id"], slug, str(src), t0)
        db.close_part(p["id"], t0 + 40, src.stat().st_size)
    finalize_recordings(cfg, db, s)
    cands = [{"id": 1, "slug": "westcol", "nombre": "Westcol", "start_ts": t0, "end_ts": t0 + 40, "duracion": 40.0,
              "transcripcion": []},
             {"id": 2, "slug": "gearofnos", "nombre": "Gear of Nos", "start_ts": t0, "end_ts": t0 + 40,
              "duracion": 40.0, "transcripcion": []}]
    cfg["edicion"]["resumen_tiktok_max_s"] = 20
    decision = {"guion": [], "mejores_momentos": [], "resumen_tiktok": [
        {"candidato_id": 1, "inicio": 2, "fin": 14, "texto_en_pantalla": "WESTCOL LO DIJO", "momento_clave": 8},
        {"candidato_id": 2, "inicio": 5, "fin": 20, "texto_en_pantalla": "GEAR RESPONDE", "momento_clave": 0}]}
    out = editor.render_tiktok_summary(cfg, db, s, decision, cands)
    from clipmax.tools import probe_duration, probe_video_size
    assert out and out.name == "resumen_tiktok_2026-09-25.mp4"
    assert probe_video_size(out) == (1080, 1920)
    assert probe_duration(out) == pytest.approx(20, abs=1.0)      # 12 s + 8 s (el segundo tramo se recorta)
    assert not (out.parent / "render_tiktok").exists()
