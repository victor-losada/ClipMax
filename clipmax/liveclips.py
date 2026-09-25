"""Clips verticales para TikTok que se arman MIENTRAS se graba.

Cada `intervalo_s` se revisan los mejores momentos que va encontrando la detección en vivo.
Cuando uno ya terminó y está grabado (con un margen), se:

1. transcribe con el modelo de calidad (subtítulos con tiempos por palabra);
2. decide el corte, el título, el caption y los hashtags: con Claude (clips_vivo.modelo, barato)
   o, si no hay API o presupuesto, con reglas simples alrededor del pico del chat;
3. renderiza en 1080x1920 con los mismos efectos del resumen (subtítulos, zoom, sonido);
4. deja el MP4, una miniatura y el caption en data/sesiones/<fecha>/clips_vivo/ y en la web.

Los procesos (whisper, ffmpeg) corren con prioridad baja para no quitarle CPU a la grabación.
Tope de clips por hora; los momentos que se solapan con un clip ya hecho se saltan.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import brain, editor, effects, style, tools, xcontext
from .cards import render_lower_third
from .config import pair_slugs, session_dir, streamer_name
from .db import Database
from .detector import describe_components
from .recorder import locate_range, snapshot_from_file
from .timeutil import fmt_clock, now
from .transcriber import WhisperTranscriber

log = logging.getLogger(__name__)

SAFE_LAG_S = 35.0     # el tramo tiene que haber terminado hace esto (grabado y con el chat ya reaccionado)
VERTICAL = (1080, 1920)


def clips_dir(cfg: dict, fecha: str) -> Path:
    d = session_dir(cfg, fecha) / "clips_vivo"
    d.mkdir(exist_ok=True)
    return d


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def hashtag_list(cfg: dict, tags, slug: str) -> list[str]:
    from .prompts import _hashtag

    out: list[str] = []
    for t in list(tags or []) + [_hashtag(cfg["evento"]["nombre"]), slug]:
        t = "#" + "".join(ch for ch in str(t).strip().lstrip("#") if ch.isalnum() or ch == "_")
        if len(t) > 1 and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out[:6]


def fit_window(cfg: dict, a: float, b: float, dur: float, key: float | None) -> tuple[float, float]:
    """Ajusta [a, b] a la duración permitida sin dejar afuera el remate."""
    lo, hi = float(cfg["clips_vivo"]["duracion_min_s"]), float(cfg["clips_vivo"]["duracion_max_s"])
    a, b = max(0.0, min(a, dur)), max(0.0, min(b, dur))
    if b <= a:
        a, b = max(0.0, dur - hi), dur
    if b - a > hi:
        if key is not None and a <= key <= b:
            b = min(b, key + 6.0)
        a = max(a, b - hi)
    if b - a < lo:
        grow = lo - (b - a)
        a = max(0.0, a - grow * 0.7)
        b = min(dur, a + max(lo, b - a))
        a = max(0.0, min(a, b - lo))
    return round(a, 2), round(b, 2)


def normalize_decision(cfg: dict, raw: dict, cand: dict) -> dict:
    """Lo que devolvió Claude, validado: tiempos dentro del tramo, duración permitida, textos cortos."""
    dur = cand["duracion"]
    key = _num(raw.get("momento_clave"), -1.0)
    key = key if 0.0 <= key <= dur else None
    a, b = fit_window(cfg, _num(raw.get("inicio")), _num(raw.get("fin"), dur), dur, key)
    if key is not None and not (a <= key <= b):
        key = None
    sfx = str(raw.get("efecto_sonido") or "").strip().lower()
    return {
        "publicar": bool(raw.get("publicar", True)),
        "motivo": str(raw.get("motivo") or "").strip()[:300],
        "inicio": a, "fin": b, "momento_clave": key,
        "titulo": str(raw.get("titulo") or "").strip()[:60] or cand["nombre"].upper(),
        "caption": str(raw.get("caption") or "").strip()[:220],
        "hashtags": hashtag_list(cfg, raw.get("hashtags"), cand["slug"]),
        "efecto_sonido": sfx if sfx in effects.sfx_names(cfg) else "",
        "emociones": [e for e in raw.get("emociones") or [] if isinstance(e, dict) and 0 <= _num(e.get("t"), -1) <= dur],
        "zoom_texto": [z for z in raw.get("zoom_texto") or [] if isinstance(z, dict)
                       and 0 <= _num(z.get("t"), -1) <= dur],
    }


def heuristic_decision(cfg: dict, db: Database, session: dict, cand: dict, moment: dict) -> dict:
    """Sin Claude: el remate es el pico del chat (menos su reacción) y el clip termina poco después."""
    dur = cand["duracion"]
    key = effects.auto_moment(db, session["id"], cand["slug"], cand["start_ts"], dur)
    if key is None:
        key = max(0.0, dur - float(cfg["deteccion"]["post_s"]))
    target = min(max(35.0, float(cfg["clips_vivo"]["duracion_min_s"])), float(cfg["clips_vivo"]["duracion_max_s"]))
    a, b = fit_window(cfg, key - target + 8.0, key + 8.0, dur, key)
    quote = ""
    for s0, s1, text in cand["transcripcion"]:
        if s0 - 1.0 <= key <= s1 + 1.0 and text.strip():
            quote = text.strip()
    pair = pair_slugs(cfg)
    if quote:
        words = quote.split()
        title = "«" + " ".join(words[:7]).upper().rstrip(".,;") + ("…»" if len(words) > 7 else "»")
    elif moment.get("componentes", {}).get("pareja") and len(pair) == 2:
        title = f"{streamer_name(cfg, pair[0])} VS {streamer_name(cfg, pair[1])}".upper()
    else:
        title = f"{cand['nombre']} EN EL {cfg['evento']['nombre']}".upper()
    caption = f"{cand['nombre']} en el {cfg['evento']['nombre']}" + (f": {quote[:110]}" if quote else " 🔥")
    return {"publicar": True, "motivo": "reglas automáticas (sin Claude)", "inicio": a, "fin": b,
            "momento_clave": key if a <= key <= b else None, "titulo": title[:60], "caption": caption[:220],
            "hashtags": hashtag_list(cfg, ["kick", "minecraft"], cand["slug"]), "efecto_sonido": ""}


def clip_material(cfg: dict, db: Database, session: dict, cand: dict, moment: dict) -> str:
    """Mensaje para Claude: quién, cuándo, por qué se detectó, qué se dijo y qué decía el chat."""
    pair = pair_slugs(cfg)
    lines = [
        f"Streamer: {cand['nombre']} (kick.com/{cand['slug']})"
        + (" · pareja principal" if cand["slug"] in pair else ""),
        f"Ventana: {cand['duracion']:.0f} s, de {fmt_clock(cand['start_ts'], cfg)} a "
        f"{fmt_clock(cand['end_ts'], cfg)} (hora de Colombia).",
        f"Por qué se detectó: {describe_components(cfg, moment.get('componentes') or {})}",
    ]
    lore = (cfg["evento"].get("lore_base") or "").strip()
    if lore:
        lines.append(f"Notas del evento: {lore[:600]}")
    sess = db.get_session(session["id"]) or session
    ctx = (sess.get("x_contexto") or "").strip()
    if ctx:
        temas = xcontext.sections(ctx).get("TEMAS") or ", ".join(k for k, _ in xcontext.keywords([ctx])[:10])
        lines.append(f"Temas en X hoy: {temas[:300]}")
    lines += ["", "Transcripción (segundos desde el inicio de la ventana):"]
    if cand["transcripcion"]:
        lines += [f"[{a:.1f}–{b:.1f}] {t}" for a, b, t in cand["transcripcion"]]
    else:
        lines.append("(sin transcripción)")
    chat = db.chat_sample(session["id"], cand["slug"], cand["start_ts"] + 5, cand["end_ts"] + 10, 12)
    if chat:
        lines += ["", "Chat en ese momento (lo más repetido): "
                  + "; ".join(f"«{c['texto'][:60]}» ×{c['n']}" for c in chat)]
    return "\n".join(lines)


class LiveClipper(threading.Thread):
    def __init__(self, cfg: dict, db: Database, session: dict):
        super().__init__(name="clips-vivo", daemon=True)
        self.cfg, self.db, self.session = cfg, db, session
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._requests: list[int] = []
        self._status: dict = {"estado": "esperando", "detalle": "", "hechos": 0}
        self._sfx: dict | None = None

    # -- estado / control ---------------------------------------------------
    @property
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def request(self, moment_id: int) -> None:
        """Clip pedido a mano desde el Panel: salta el tope por hora."""
        with self._lock:
            if moment_id not in self._requests:
                self._requests.append(moment_id)
        self._wake.set()

    def run(self) -> None:
        # Si ClipMax se cerró a mitad de un clip, ese quedó "procesando": no debe contar para el tope.
        self.db.execute("UPDATE live_clips SET estado='error', nota='interrumpido (ClipMax se cerró)' "
                        "WHERE session_id=? AND estado='procesando'", (self.session["id"],))
        interval = float(self.cfg["clips_vivo"]["intervalo_s"])
        while not self._stop_evt.is_set():
            self._wake.wait(interval)
            self._wake.clear()
            if self._stop_evt.is_set():
                break
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - un clip roto no debe parar los siguientes
                log.exception("Clips en vivo: %s", exc)
                self._set(estado="error", detalle=str(exc)[-200:])

    # -- lógica ---------------------------------------------------------------
    def step(self) -> int | None:
        cv = self.cfg["clips_vivo"]
        sid = self.session["id"]
        moments = {m["id"]: m for m in self.db.moments(sid)}
        with self._lock:
            pending = [moments[i] for i in self._requests if i in moments]
            self._requests = [i for i in self._requests if i in moments]
        ripe = [m for m in pending if m["end_ts"] <= now() - SAFE_LAG_S]
        if ripe:
            with self._lock:
                self._requests.remove(ripe[0]["id"])
            return self.make(ripe[0])
        if not cv["activo"]:
            return None
        recent = [c for c in self.db.live_clips(sid, ("listo", "procesando"))
                  if c["created_at"] >= now() - 3600]
        if len(recent) >= cv["max_por_hora"]:
            self._set(estado="esperando", detalle=f"tope de {cv['max_por_hora']} clips por hora")
            return None
        m = self.pick()
        if not m:
            self._set(estado="esperando", detalle="buscando el próximo momento con hype")
            return None
        return self.make(m)

    def pick(self) -> dict | None:
        cv = self.cfg["clips_vivo"]
        done = self.db.live_clips(self.session["id"])
        horizon = now() - SAFE_LAG_S
        for m in self.db.moments(self.session["id"], limit=int(cv["top_candidatos"])):
            if m["end_ts"] > horizon:
                continue
            length = m["end_ts"] - m["start_ts"]
            if any(c["slug"] == m["slug"] and _overlap(c["start_ts"], c["end_ts"], m["start_ts"], m["end_ts"])
                   > 0.3 * min(length, c["end_ts"] - c["start_ts"]) for c in done):
                continue
            return m
        return None

    def make(self, moment: dict) -> int:
        cfg, db = self.cfg, self.db
        name = streamer_name(cfg, moment["slug"])
        cid = db.add_live_clip(session_id=self.session["id"], slug=moment["slug"], start_ts=moment["start_ts"],
                               end_ts=moment["end_ts"], score=float(moment.get("score") or 0), estado="procesando")
        self._set(estado="procesando", detalle=f"{name} · {fmt_clock(moment['start_ts'], cfg, seconds=False)}")
        t0 = time.time()
        try:
            with tools.background_priority():
                result = self._build(cid, moment)
        except Exception as exc:  # noqa: BLE001
            log.warning("Clip en vivo de %s falló: %s", name, exc)
            db.update_live_clip(cid, estado="error", nota=str(exc)[-300:])
            self._set(estado="error", detalle=f"{name}: {str(exc)[-160:]}")
            return cid
        if result == "listo":
            with self._lock:
                self._status["hechos"] = self._status.get("hechos", 0) + 1
            log.info("Clip en vivo listo: %s (%.0fs de proceso)", name, time.time() - t0)
        self._set(estado="esperando", detalle=f"último: {name} ({result})")
        return cid

    def _build(self, cid: int, moment: dict) -> str:
        cfg, db, session = self.cfg, self.db, self.session
        sid, slug = session["id"], moment["slug"]
        loc = locate_range(db, sid, slug, moment["start_ts"], moment["end_ts"], float(cfg["grabacion"]["desfase_chat_s"]))
        if not loc:
            raise RuntimeError("no hay video grabado para ese tramo")
        path, start, dur, wall0 = loc
        # 1) Transcripción de calidad (también queda para el resumen del día).
        try:
            segs = WhisperTranscriber(cfg).transcribe_window(path, start, dur, "calidad")
            abs_segs = [(wall0 + s.start, wall0 + s.end, s.text, [[wall0 + a, wall0 + b, w] for a, b, w in s.words])
                        for s in segs if s.text]
            db.delete_segments(sid, slug, wall0 - 1, wall0 + dur + 1, "candidato")
            db.add_segments(sid, slug, abs_segs, "candidato")
        except tools.ToolMissing as exc:
            log.warning("Clip en vivo sin transcripción: %s", exc)
        segs_db = db.segments(sid, slug, wall0, wall0 + dur, "candidato") or db.segments(sid, slug, wall0, wall0 + dur, "vivo")
        cand = {"id": 0, "slug": slug, "nombre": streamer_name(cfg, slug), "start_ts": wall0, "end_ts": wall0 + dur,
                "duracion": dur,
                "transcripcion": [[round(max(0.0, s["start_ts"] - wall0), 1), round(min(dur, s["end_ts"] - wall0), 1),
                                   s["texto"].strip()] for s in segs_db if s["texto"].strip()]}
        # 2) Decisión editorial.
        decision, origen = None, "auto"
        if cfg["clips_vivo"]["usar_claude"] and cfg["claude"]["modo"] == "api":
            try:
                raw = brain.curate_live_clip(cfg, db, session, clip_material(cfg, db, session, cand, moment))
                decision, origen = normalize_decision(cfg, raw, cand), "claude"
            except (brain.BudgetExceeded, brain.ClaudeError) as exc:
                log.warning("Clip en vivo sin Claude (%s); uso reglas automáticas", exc)
        if decision is None:
            decision = heuristic_decision(cfg, db, session, cand, moment)
        common = {"origen": origen, "titulo": decision["titulo"], "caption": decision["caption"],
                  "hashtags": decision["hashtags"], "nota": decision["motivo"]}
        if not decision["publicar"]:
            db.update_live_clip(cid, estado="descartado", **common)
            return "descartado"
        # 3) Render vertical con efectos.
        spec = editor.prepare_clip(cfg, db, session, cand, decision["inicio"], decision["fin"], decision["titulo"])
        if not spec:
            raise RuntimeError("no se pudo ubicar el tramo elegido")
        key = decision["momento_clave"]
        if key is not None:
            rel = wall0 + key - spec.wall0
            spec.momento = rel if 0 <= rel <= spec.dur else None
        if spec.momento is None and cfg["edicion"].get("zoom_auto"):
            spec.momento = effects.auto_moment(db, sid, slug, spec.wall0, spec.dur)
        spec.efecto = decision["efecto_sonido"]
        # Punch-ins a la cara en las emociones y zoom al texto que las provoca (style.py).
        style.direct(cfg, db, session, spec, cand,
                     {"momento_clave": key or 0, "emociones": decision.get("emociones", []),
                      "zoom_texto": decision.get("zoom_texto", [])}, VERTICAL)
        folder = clips_dir(cfg, session["fecha"])
        stem = f"{cid:03d}_{fmt_clock(wall0, cfg, seconds=False).replace(':', '')}_{slug}_{editor.slugify(decision['titulo'], 30)}"
        out = folder / f"{stem}.mp4"
        work = folder / f"_render_{cid}"
        work.mkdir(exist_ok=True)
        if self._sfx is None and cfg["edicion"].get("efectos_sonido"):
            self._sfx = effects.sfx_library(cfg)
        try:
            title_png = None
            if cfg["edicion"]["titulos_en_pantalla"]:
                title_png = render_lower_third(decision["titulo"], spec.nombre, VERTICAL, work / "titulo.png",
                                               cfg["edicion"]["fuente"])
            kept = editor.render_clip(cfg, spec, out, VERTICAL, title_png, work=work, sfx_lib=self._sfx)
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        thumb = folder / f"{stem}.jpg"
        try:
            snapshot_from_file(str(out), min(2.0, kept / 2), thumb, width=270)
        except Exception:  # noqa: BLE001 - la miniatura es opcional
            thumb = None
        db.update_live_clip(cid, estado="listo", path=str(out), thumb=str(thumb) if thumb else None,
                            duracion=round(kept, 1), **common)
        db.add_output(sid, "clip_vivo", str(out), {"titulo": decision["titulo"]})
        return "listo"
