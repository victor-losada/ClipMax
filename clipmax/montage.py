"""Montaje del resumen diario en estilo "Eufonía" (edicion.estilo = "eufonia").

Estructura (prompts/estilo_eufonia.md): gancho en frío (una frase gritada repetida 2-3 veces
con zoom creciente a la cara) -> sting de 4 s con el título en pixel y la voz abajo -> bloques
(premisa, cuerpo, subida, pausa, clímax, desenlace, cierre) con cortes secos, captions de frases
clave, punch-ins y zoom a los textos que provocan reacciones -> pantalla final en pixel.

Sin música ni transiciones. Silencios de más de 1.5 s recortados (salvo pausas pedidas), el
volumen de cada bloque sube hacia el clímax y como mucho 3 tarjetas de narración.
"""

from __future__ import annotations

import copy
import logging
import shutil
from pathlib import Path

from . import effects, style, tools
from .cards import render_end_card, render_pixel_label, render_sting_title
from .config import session_dir
from .db import Database
from .editor import (ClipSpec, apply_clip_effects, budget_sfx, concat_pieces, output_size, prepare_clip,
                     render_card_piece, render_clip)
from .recorder import snapshot_from_file

log = logging.getLogger(__name__)

STING_S = 4.0
END_S = 4.5
HOOK_ZOOM = {2: [1.0, 1.4], 3: [1.0, 1.25, 1.5]}


def tighten_hook(spec: ClipSpec) -> None:
    """El gancho es una frase gritada de 1-3 s: dentro del tramo que eligió Claude (que ve la
    transcripción por frases) se busca el grito o la exclamación más fuerte y se deja solo eso,
    cortando en límites de palabra."""
    try:
        env = style.loudness(spec.path, spec.file_start, spec.dur)
    except Exception:  # noqa: BLE001
        env = []
    marks = style.shout_marks(env) + style.lexicon_marks(spec.words, env)
    t = max(marks, key=lambda m: m.strength).t if marks else 0.0
    words = [w for w in spec.words if w[0] >= t - 0.3]
    a = max(0.0, (words[0][0] if words else t) - 0.15)
    b = a + 2.6
    ends = [w[1] for w in words if w[1] <= b]
    if ends:
        b = ends[-1] + 0.25
    b = min(spec.dur, max(a + 1.0, b))
    spec.keep = [(round(a, 3), round(b, 3))]


def _hook_variant(spec: ClipSpec, factor: float, anchor: tuple[float, float]) -> ClipSpec:
    """El gancho repetido: el mismo tramo con un zoom fijo cada vez mayor a la cara."""
    s = copy.copy(spec)
    base = spec.plan or style.Plan()
    plan = copy.copy(base)
    plan.facecam = []
    plan.zooms = [style.Zoom(0.0, spec.kept_duration + 1.0, factor, anchor[0], anchor[1], "fijo")] if factor > 1 else []
    s.plan = plan
    return s


def render_summary_eufonia(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                           progress=None) -> Path:
    fecha = session["fecha"]
    work = session_dir(cfg, fecha) / "render"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    size = output_size(cfg)
    by_id = {int(c["id"]): c for c in candidates}
    ed = cfg["edicion"]
    items = decision["guion"]
    gap = min(1.5, float(ed["silencio_max_s"]))

    # 1) Tramos: dónde está cada clip en disco y su plan de efectos.
    specs: dict[int, ClipSpec] = {}
    facecam_used = 0
    body_minutes = sum(it["fin"] - it["inicio"] for it in items if it["tipo"] == "clip") / 60
    facecam_budget = max(3, int(body_minutes / 3))
    for idx, it in enumerate(items):
        if it["tipo"] not in ("clip", "gancho"):
            continue
        cand = by_id.get(int(it["candidato_id"]))
        if not cand:
            continue
        bloque = it.get("bloque") or ("gancho" if it["tipo"] == "gancho" else "cuerpo")
        keep_all = it["tipo"] == "gancho" or bool(it.get("conservar_silencios")) or bloque == "pausa"
        spec = prepare_clip(cfg, db, session, cand, it["inicio"], it["fin"], "", max_gap=gap, keep_all=keep_all)
        if not spec:
            continue
        spec.bloque = bloque
        spec.loud_target = style.VOLUMEN.get(bloque, -16.5)
        if it["tipo"] == "gancho" and spec.dur > 3.2:
            tighten_hook(spec)
        apply_clip_effects(cfg, db, session, spec, cand, it, by_id)
        plan = style.direct(cfg, db, session, spec, cand, it, size, captions=True,
                            allow_facecam=facecam_used < facecam_budget)
        if plan and plan.facecam:
            facecam_used += len(plan.facecam)
        if it.get("rotulo"):
            spec.titulo = it["rotulo"]
            spec.title_hold = max(10.0, spec.kept_duration)
        specs[idx] = spec
    budget_sfx(cfg, list(specs.values()))
    sfx_lib = effects.sfx_library(cfg) if ed.get("efectos_sonido") and any(s.efecto for s in specs.values()) else None

    pieces: list[Path] = []

    def add(path: Path) -> None:
        pieces.append(path)

    def render(spec: ClipSpec, name: str, title_png: Path | None = None) -> None:
        out = work / f"{len(pieces):03d}_{name}.mp4"
        try:
            render_clip(cfg, spec, out, size, title_png, work=work, sfx_lib=sfx_lib)
            add(out)
        except Exception as exc:  # noqa: BLE001 - una pieza rota no tumba el video
            log.error("Falló la pieza %s: %s", name, exc)

    # 2) Gancho: la frase repetida con zoom creciente a la cara.
    hooks = [i for i, it in enumerate(items) if it["tipo"] == "gancho" and i in specs]
    for i in hooks[:1]:
        spec = specs[i]
        geo_anchor = (0.5, 0.5)
        if spec.plan is not None:
            from .config import get_streamer
            from .editor import _src_size
            st = get_streamer(cfg, spec.slug) or {}
            geo_anchor = style.Geometry(_src_size(spec.path), size, st.get("modo", "juego_cara"),
                                        st.get("camara")).cam_anchor()
        reps = int(items[i].get("repeticiones") or 3)
        for r, factor in enumerate(HOOK_ZOOM.get(reps, HOOK_ZOOM[3])):
            if progress:
                progress(f"gancho {r + 1}/{reps}")
            render(_hook_variant(spec, factor, geo_anchor), f"gancho{r}")

    # 3) Sting: 4 s del primer clip del cuerpo con la voz abajo y el título del día en pixel.
    first = next((specs[i] for i, it in enumerate(items) if it["tipo"] == "clip" and i in specs), None)
    if first:
        sting = copy.copy(first)
        sting.keep = [(0.0, min(STING_S, first.dur))]
        sting.plan, sting.efecto, sting.whoosh, sting.partner = style.Plan(captions=[]), "", False, None
        sting.audio_gain, sting.loud_target, sting.title_hold = 0.18, -20.0, STING_S
        png = render_sting_title(cfg["evento"]["nombre"], decision.get("titulo_video", ""), size, work / "sting.png")
        if progress:
            progress("sting")
        render(sting, "sting", png)

    # 4) Cuerpo: clips (con rótulo pixel en el desenlace) y hasta 3 tarjetas de narración.
    last_clip: ClipSpec | None = None
    for idx, it in enumerate(items):
        if progress:
            progress(f"render {idx + 1}/{len(items)}: {it['tipo']}")
        if it["tipo"] == "narracion":
            nxt = next((specs[j] for j in range(idx + 1, len(items)) if j in specs), last_clip)
            bg = None
            if nxt:
                bg = work / f"bg_{idx:03d}.jpg"
                try:
                    snapshot_from_file(nxt.path, nxt.file_start + 1.0, bg, width=size[0])
                except Exception:  # noqa: BLE001
                    bg = None
            out = work / f"{len(pieces):03d}_narracion.mp4"
            try:
                render_card_piece(cfg, it["texto"], out, size, bg=bg, label=cfg["evento"]["nombre"], eufonia=True)
                add(out)
            except Exception as exc:  # noqa: BLE001
                log.error("Falló la narración %d: %s", idx, exc)
            continue
        if it["tipo"] != "clip" or idx not in specs:
            continue
        spec = specs[idx]
        png = render_pixel_label(it["rotulo"], size, work / f"rotulo_{idx:03d}.png") if it.get("rotulo") else None
        render(spec, f"clip{idx}", png)
        last_clip = spec

    if not any(p.name.split("_", 1)[1].startswith("clip") for p in pieces):
        raise RuntimeError("No se pudo renderizar ningún clip; revisa el log")

    # 5) Pantalla final en pixel sobre el último plano, en silencio.
    if last_clip:
        bg = work / "bg_final.jpg"
        try:
            snapshot_from_file(last_clip.path, last_clip.file_start + max(0.0, last_clip.dur - 1.0), bg, width=size[0])
        except Exception:  # noqa: BLE001
            bg = None
        end_png = render_end_card("Suscríbete", f"{cfg['evento']['nombre']} · {fecha}", size, work / "final.png", bg)
        out = work / f"{len(pieces):03d}_final.mp4"
        render_card_piece(cfg, "", out, size, png=end_png, dur=END_S, voice=False)
        add(out)

    final = session_dir(cfg, fecha) / f"resumen_{fecha}.mp4"
    if progress:
        progress("uniendo piezas")
    concat_pieces(pieces, final)
    real = tools.probe_duration(final)
    if real < float(ed["duracion_min_min"]) * 60:
        log.warning("El resumen dura %.1f min (< %s min objetivo)", real / 60, ed["duracion_min_min"])
    log.info("Resumen listo (estilo Eufonía): %s (%.1f min, %d piezas)", final.name, real / 60, len(pieces))
    db.add_output(session["id"], "video", str(final), {"duracion_s": round(real, 1), "piezas": len(pieces),
                                                       "estilo": "eufonia"})
    if not log.isEnabledFor(logging.DEBUG):
        shutil.rmtree(work, ignore_errors=True)
    return final
