"""Detección de momentos candidatos SIN modelos entrenados.

Señales (cada una con hora de pared y puntuación 0-1):
- pico_chat:    el chat de un stream explota respecto a su propio ritmo reciente.
                Se usa un z-score robusto (mediana + MAD de los últimos 10 min),
                así un chat de 50k espectadores y uno de 2k se miden con la misma vara.
                Bonus si los mensajes son de risa/sorpresa/"clip".
- mencion_chat: en el chat de A se dispara la cantidad de mensajes que nombran a B.
- mencion_voz:  A nombra a B en su audio (whisper en vivo o en el candidato).
- sincronia:    A y B tienen pico de chat casi al mismo tiempo (están interactuando).
- tema_x:       la transcripción toca los temas que se discuten hoy en X
                (se aplica al re-puntuar, cuando ya hay transcripción).

Las señales de un mismo streamer que se solapan se funden en un "momento":
una ventana [inicio, fin] con puntuación = suma ponderada de sus señales, con
multiplicador extra si involucra a la pareja principal (Westcol <-> Gear of Nos).
"""

from __future__ import annotations

import collections
import logging
import statistics
from typing import Iterable

from .config import active_streamers, pair_slugs, streamer_name
from .db import Database
from .mentions import MentionMatcher, normalize

log = logging.getLogger(__name__)

COMPUTED_TYPES = ("pico_chat", "mencion_chat", "sincronia")


# ---------------------------------------------------------------------------
# Series de tiempo
# ---------------------------------------------------------------------------

def dense_series(rows: list[dict], bucket_s: float, fields: Iterable[str]) -> tuple[float, dict[str, list[float]]]:
    """Convierte buckets dispersos (solo los que tuvieron mensajes) en series densas con ceros."""
    fields = list(fields)
    if not rows:
        return 0.0, {f: [] for f in fields}
    t0 = rows[0]["bucket_ts"]
    n = int(round((rows[-1]["bucket_ts"] - t0) / bucket_s)) + 1
    out = {f: [0.0] * n for f in fields}
    for r in rows:
        i = int(round((r["bucket_ts"] - t0) / bucket_s))
        if 0 <= i < n:
            for f in fields:
                out[f][i] += float(r[f])
    return t0, out


def moving_avg(values: list[float], k: int) -> list[float]:
    if k <= 1 or not values:
        return list(values)
    half = k // 2
    out = []
    acc = collections.deque()
    total = 0.0
    # Ventana centrada: [i-half, i+half]
    padded = [values[0]] * half + values + [values[-1]] * half
    for i, v in enumerate(padded):
        acc.append(v)
        total += v
        if len(acc) > k:
            total -= acc.popleft()
        if i >= k - 1:
            out.append(total / k)
    return out


def nms(items: list[dict], key_ts: str, key_score: str, min_sep: float) -> list[dict]:
    """Non-maximum suppression: de varios picos cercanos queda el más fuerte."""
    kept: list[dict] = []
    for it in sorted(items, key=lambda x: -x[key_score]):
        if all(abs(it[key_ts] - k[key_ts]) >= min_sep for k in kept):
            kept.append(it)
    return sorted(kept, key=lambda x: x[key_ts])


# ---------------------------------------------------------------------------
# Señales
# ---------------------------------------------------------------------------

def detect_chat_peaks(rows: list[dict], p: dict) -> list[dict]:
    """rows: chat_buckets de un streamer. Devuelve [{ts, score, z, ratio, msgs, hype_frac}]."""
    bs = float(p["bucket_s"])
    t0, series = dense_series(rows, bs, ("n_msgs", "n_hype"))
    counts, hype = series["n_msgs"], series["n_hype"]
    if not counts:
        return []
    k = max(1, int(p["suavizado_buckets"]))
    sm = moving_avg(counts, k)
    win = max(12, int(float(p["ventana_base_min"]) * 60 / bs))
    thr = float(p["umbral_z"])
    found = []
    for i in range(len(sm)):
        lo = max(0, i - win)
        base = sm[lo:max(lo, i - k)]  # historia previa, sin el propio pico
        if len(base) < max(6, win // 4):
            continue
        med = statistics.median(base)
        mad = statistics.median(abs(x - med) for x in base)
        z = (sm[i] - med) / (1.4826 * mad + 1.0)
        ratio = sm[i] / max(med, 0.5)
        if z < thr or ratio < float(p["ratio_min"]) or counts[i] < float(p["mensajes_min_bucket"]):
            continue
        j0, j1 = max(0, i - k), min(len(counts), i + k + 1)
        msgs = sum(counts[j0:j1])
        hype_frac = sum(hype[j0:j1]) / msgs if msgs else 0.0
        intensity = min(1.0, 0.4 + (z - thr) / (2 * thr))
        score = min(1.0, 0.75 * intensity + 0.3 * min(1.0, hype_frac / 0.5))
        found.append({"ts": t0 + i * bs, "score": round(score, 3), "z": round(z, 2),
                      "ratio": round(ratio, 2), "msgs": int(msgs), "hype_frac": round(hype_frac, 2)})
    return nms(found, "ts", "score", float(p["separacion_picos_s"]))


def detect_chat_mention_spikes(rows: list[dict], p: dict) -> list[dict]:
    """rows: chat_mentions de UN chat. Devuelve [{ts, target, score, n}] por objetivo."""
    bs = float(p["bucket_s"])
    per_target: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        per_target[r["target"]].append(r)
    out = []
    win = max(1, int(30 / bs))  # suma móvil de 30 s
    for target, trows in per_target.items():
        t0, series = dense_series(trows, bs, ("n",))
        vals = series["n"]
        if not vals:
            continue
        sums = [sum(vals[max(0, i - win + 1):i + 1]) for i in range(len(vals))]
        baseline = statistics.mean(sums) if sums else 0.0
        need = max(float(p["umbral_menciones_chat"]) * 2, baseline * 3)
        found = [
            {"ts": t0 + i * bs, "target": target, "n": int(s),
             "score": round(min(1.0, s / (need * 3)) * 0.7 + 0.3, 3)}
            for i, s in enumerate(sums) if s >= need
        ]
        out += nms(found, "ts", "score", float(p["separacion_picos_s"]))
    return sorted(out, key=lambda x: x["ts"])


def detect_sync(peaks_by_slug: dict[str, list[dict]], window: float) -> list[dict]:
    """Picos de chat simultáneos en dos streams -> probable interacción entre ellos."""
    out = []
    slugs = sorted(peaks_by_slug)
    for ai, a in enumerate(slugs):
        for b in slugs[ai + 1:]:
            for pa in peaks_by_slug[a]:
                for pb in peaks_by_slug[b]:
                    if abs(pa["ts"] - pb["ts"]) <= window:
                        s = round(min(pa["score"], pb["score"]), 3)
                        out.append({"slug": a, "ts": pa["ts"], "score": s, "con": b})
                        out.append({"slug": b, "ts": pb["ts"], "score": s, "con": a})
    return out


# ---------------------------------------------------------------------------
# Momentos
# ---------------------------------------------------------------------------

def _window_for(sig: dict, p: dict) -> tuple[float, float]:
    pre, post = float(p["pre_s"]), float(p["post_s"])
    tipo = sig["tipo"]
    if tipo == "mencion_voz":
        return sig["ts"] - 25, sig["ts"] + 45   # quien habla arranca el tema; el contexto viene después
    if tipo == "mencion_chat":
        return sig["ts"] - pre, sig["ts"] + post / 2
    return sig["ts"] - pre, sig["ts"] + post


def _targets_of(sig: dict) -> set[str]:
    d = sig.get("detalle") or {}
    return {t for t in (d.get("target"), d.get("con")) if t}


def score_moment(signals: list[dict], slug: str, p: dict, priority: float, pair: list[str]) -> tuple[float, dict]:
    weights = p["pesos"]
    seen: collections.Counter = collections.Counter()
    score = 0.0
    targets: set[str] = set()
    comp: dict = {"senales": collections.Counter(), "max_z": 0.0, "con": [], "textos_voz": []}
    for s in sorted(signals, key=lambda x: -x["score"]):
        # Rendimientos decrecientes: la 2ª señal del mismo tipo vale la mitad, la 3ª un cuarto...
        factor = 0.5 ** seen[s["tipo"]]
        seen[s["tipo"]] += 1
        score += float(weights.get(s["tipo"], 1.0)) * s["score"] * factor
        comp["senales"][s["tipo"]] += 1
        targets |= _targets_of(s)
        d = s.get("detalle") or {}
        comp["max_z"] = max(comp["max_z"], float(d.get("z") or 0))
        if s["tipo"] == "mencion_voz" and d.get("texto") and len(comp["textos_voz"]) < 3:
            comp["textos_voz"].append(d["texto"])
    comp["senales"] = dict(comp["senales"])
    comp["con"] = sorted(targets)
    is_pair = slug in pair and any(t in pair and t != slug for t in targets)
    comp["pareja"] = is_pair
    if is_pair:
        score *= float(p["multiplicador_pareja"])
    score *= priority
    return round(score, 3), comp


def build_moments(signals: list[dict], p: dict, priorities: dict[str, float], pair: list[str]) -> list[dict]:
    max_dur = float(p["duracion_max_candidato_s"])
    by_slug: dict[str, list[dict]] = collections.defaultdict(list)
    for s in signals:
        by_slug[s["slug"]].append(s)
    moments = []
    for slug, sigs in by_slug.items():
        spans = sorted(((*_window_for(s, p), s) for s in sigs), key=lambda x: x[0])
        group: list[dict] = []
        g_start = g_end = None
        for start, end, sig in spans:
            if group and start <= g_end and max(end, g_end) - g_start <= max_dur:
                group.append(sig)
                g_end = max(g_end, end)
                continue
            if group:
                moments.append((slug, g_start, g_end, group))
            group, g_start, g_end = [sig], start, end
        if group:
            moments.append((slug, g_start, g_end, group))
    out = []
    for slug, start, end, group in moments:
        score, comp = score_moment(group, slug, p, priorities.get(slug, 1.0), pair)
        out.append({"slug": slug, "start_ts": start, "end_ts": end, "score": score, "componentes": comp})
    return out


def mark_shared(moments: list[dict]) -> None:
    """Momentos de distintos streamers que se solapan = el mismo suceso visto desde dos cámaras."""
    ordered = sorted(moments, key=lambda m: m["start_ts"])
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b["start_ts"] > a["end_ts"]:
                break
            if a["slug"] == b["slug"]:
                continue
            overlap = min(a["end_ts"], b["end_ts"]) - max(a["start_ts"], b["start_ts"])
            if overlap >= 20:
                a["componentes"].setdefault("mismo_suceso", [])
                b["componentes"].setdefault("mismo_suceso", [])
                if b["slug"] not in a["componentes"]["mismo_suceso"]:
                    a["componentes"]["mismo_suceso"].append(b["slug"])
                if a["slug"] not in b["componentes"]["mismo_suceso"]:
                    b["componentes"]["mismo_suceso"].append(a["slug"])


def rank(moments: list[dict], limit: int) -> list[dict]:
    moments = sorted(moments, key=lambda m: -m["score"])[:limit]
    for i, m in enumerate(moments, 1):
        m["rank"] = i
    return moments


# ---------------------------------------------------------------------------
# Orquestación contra la base de datos
# ---------------------------------------------------------------------------

def run_detection(cfg: dict, db: Database, session: dict, store_signals: bool = True) -> list[dict]:
    p = cfg["deteccion"]
    # Todos los que tengan datos esta sesión (aunque se hayan desactivado después de grabarlos).
    slugs = sorted({s["slug"] for s in active_streamers(cfg)}
                   | {r["slug"] for r in db.query("SELECT DISTINCT slug FROM chat_buckets WHERE session_id=?",
                                                  (session["id"],))}
                   | {p_["slug"] for p_ in db.list_parts(session["id"])})
    priorities = {s["slug"]: s["prioridad"] for s in cfg["streamers"]}
    pair = pair_slugs(cfg)
    sid = session["id"]

    signals: list[dict] = []
    peaks_by_slug: dict[str, list[dict]] = {}
    for slug in slugs:
        peaks = detect_chat_peaks(db.chat_buckets(sid, slug), p)
        peaks_by_slug[slug] = peaks
        for pk in peaks:
            signals.append({"slug": slug, "ts": pk["ts"], "tipo": "pico_chat", "score": pk["score"],
                            "detalle": {k: pk[k] for k in ("z", "ratio", "msgs", "hype_frac")}})
        for sp in detect_chat_mention_spikes(db.chat_mentions(sid, slug), p):
            signals.append({"slug": slug, "ts": sp["ts"], "tipo": "mencion_chat", "score": sp["score"],
                            "detalle": {"target": sp["target"], "n": sp["n"]}})
    for sy in detect_sync(peaks_by_slug, float(p["sincronia_s"])):
        signals.append({"slug": sy["slug"], "ts": sy["ts"], "tipo": "sincronia", "score": sy["score"],
                        "detalle": {"con": sy["con"]}})

    if store_signals:
        db.delete_signals(sid, COMPUTED_TYPES)
        for s in signals:
            db.add_signal(sid, s["slug"], s["ts"], s["tipo"], s["score"], s["detalle"])

    # Menciones por voz que dejó el transcriptor en vivo.
    signals += [s for s in db.signals(sid, ["mencion_voz"]) if s["slug"] in slugs]

    moments = build_moments(signals, p, priorities, pair)
    mark_shared(moments)
    ranked = rank(moments, int(p["candidatos_max"]))
    db.replace_moments(sid, ranked)
    log.info("Detección: %d señales -> %d momentos (%d candidatos)", len(signals), len(moments), len(ranked))
    return db.moments(sid)


def rescore_with_transcripts(cfg: dict, db: Database, session: dict, matcher: MentionMatcher,
                             x_keywords: list[str], x_bursts: list[tuple[float, int]] | None = None) -> list[dict]:
    """Con la transcripción ya hecha, ajusta la puntuación de cada candidato.

    - Menciones por voz que el en-vivo no vio (streamers sin transcripción en vivo).
    - Coincidencia con los temas de X del día.
    - Penaliza ventanas casi sin voz (gameplay callado: "Westcol picando una mina").
    """
    p = cfg["deteccion"]
    pair = pair_slugs(cfg)
    w = p["pesos"]
    sid = session["id"]
    kw = [normalize(k) for k in x_keywords if k]
    moments = db.moments(sid)
    for m in moments:
        comp = m["componentes"]
        # Idempotente: siempre se parte del estado previo al primer re-puntuado.
        base = comp.setdefault("base", {"score": m["score"], "pareja": comp.get("pareja", False),
                                        "con": comp.get("con", [])})
        m["score"] = base["score"]
        comp["pareja"] = base["pareja"]
        comp["con"] = list(base["con"])
        for key in ("temas_x", "pico_x", "poca_voz", "menciones_transcripcion"):
            comp.pop(key, None)
        segs = db.segments(sid, m["slug"], m["start_ts"], m["end_ts"])
        text = " ".join(s["texto"] for s in segs)
        norm = normalize(text)
        bonus = 0.0
        found = matcher.find_others(text, m["slug"], fuzzy=True)
        new_targets = [t for t in found if t not in comp.get("con", [])]
        if found and not comp.get("senales", {}).get("mencion_voz"):
            bonus += float(w["mencion_voz"]) * min(1.0, 0.6 + 0.2 * sum(found.values()))
        comp["menciones_transcripcion"] = found
        comp["con"] = sorted(set(comp.get("con", [])) | set(found))
        if not comp.get("pareja") and m["slug"] in pair and any(t in pair for t in new_targets):
            comp["pareja"] = True
            bonus += float(w["mencion_voz"]) * (float(p["multiplicador_pareja"]) - 1)
        hits = [k for k in kw if k and f" {k} " in f" {norm} "]
        if hits:
            bonus += float(w["tema_x"]) * min(1.0, len(hits) / 3)
            comp["temas_x"] = hits[:6]
        if x_bursts:
            for bt, n in x_bursts:
                if bt - 900 <= m["start_ts"] <= bt + 600 and (found or comp.get("con")):
                    bonus += float(w["tema_x"]) * 0.5
                    comp["pico_x"] = n
                    break
        dur_min = max(0.25, (m["end_ts"] - m["start_ts"]) / 60)
        wpm = len(norm.split()) / dur_min
        comp["palabras_min"] = round(wpm)
        factor = 1.0
        if segs and wpm < 25:
            factor = 0.6
            comp["poca_voz"] = True
        score = round((m["score"] + bonus) * factor, 3)
        db.update_moment(m["id"], score=score, componentes=comp)
    # Re-ordenar por la nueva puntuación.
    moments = sorted(db.moments(sid), key=lambda m: -m["score"])
    for i, m in enumerate(moments, 1):
        db.update_moment(m["id"], rank=i)
    return db.moments(sid)


def describe_components(cfg: dict, comp: dict) -> str:
    """Texto corto y legible de por qué un candidato puntuó alto."""
    parts = []
    s = comp.get("senales", {})
    if s.get("pico_chat"):
        parts.append(f"pico de chat (z={comp.get('max_z', 0):.1f})")
    if s.get("mencion_chat"):
        parts.append("el chat nombra a " + ", ".join(streamer_name(cfg, t) for t in comp.get("con", [])) if comp.get("con") else "menciones en el chat")
    if s.get("mencion_voz") or comp.get("menciones_transcripcion"):
        who = comp.get("menciones_transcripcion") or {}
        names = ", ".join(streamer_name(cfg, t) for t in who) or ", ".join(streamer_name(cfg, t) for t in comp.get("con", []))
        parts.append(f"menciona por voz a {names}" if names else "mención por voz")
    if s.get("sincronia"):
        parts.append("sincronía con otro stream")
    if comp.get("mismo_suceso"):
        parts.append("mismo suceso en: " + ", ".join(streamer_name(cfg, t) for t in comp["mismo_suceso"]))
    if comp.get("temas_x"):
        parts.append("temas de X: " + ", ".join(comp["temas_x"][:4]))
    if comp.get("pareja"):
        parts.append("★ involucra a la pareja principal")
    if comp.get("poca_voz"):
        parts.append("poca conversación")
    return "; ".join(parts) or "sin detalle"
