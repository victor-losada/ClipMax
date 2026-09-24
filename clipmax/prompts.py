"""Construcción del material que recibe Claude.

- prompts/prompt_maestro.md es la fuente única del prompt: lo usa la API como
  system prompt y es el mismo texto que pegas en claude.ai en modo manual.
- build_candidates() congela la lista de candidatos (id, streamer, ventana,
  transcripción...) en un JSON: la decisión de Claude se aplica siempre sobre
  esa foto, aunque después se re-ejecute la detección.
- build_day_material() redacta el mensaje del día en Markdown legible.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import PROJECT_ROOT, pair_slugs, session_dir, streamer_name
from .db import Database
from .detector import describe_components
from .timeutil import fmt_clock

MASTER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "prompt_maestro.md"

# Esquema de salida (structured outputs). Todas las propiedades son obligatorias y
# additionalProperties=false, como exige la API; los rangos se validan en brain.py.
_GUION_ITEM = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["narracion", "clip"]},
        "texto": {"type": "string"},
        "candidato_id": {"type": "integer"},
        "inicio": {"type": "number"},
        "fin": {"type": "number"},
        "titulo_en_pantalla": {"type": "string"},
        "prioridad": {"type": "integer"},
        "motivo": {"type": "string"},
    },
    "required": ["tipo", "texto", "candidato_id", "inicio", "fin", "titulo_en_pantalla", "prioridad", "motivo"],
    "additionalProperties": False,
}
_MOMENTO = {
    "type": "object",
    "properties": {
        "candidato_id": {"type": "integer"},
        "inicio": {"type": "number"},
        "fin": {"type": "number"},
        "titulo": {"type": "string"},
        "por_que_importa": {"type": "string"},
        "captions_tiktok": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["candidato_id", "inicio", "fin", "titulo", "por_que_importa", "captions_tiktok", "hashtags"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo_video": {"type": "string"},
        "resumen_del_dia": {"type": "string"},
        "lore_para_manana": {"type": "string"},
        "guion": {"type": "array", "items": _GUION_ITEM},
        "mejores_momentos": {"type": "array", "items": _MOMENTO},
        "descartados": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"candidato_id": {"type": "integer"}, "motivo": {"type": "string"}},
                "required": ["candidato_id", "motivo"],
                "additionalProperties": False,
            },
        },
        "notas_editor": {"type": "string"},
    },
    "required": ["titulo_video", "resumen_del_dia", "lore_para_manana", "guion",
                 "mejores_momentos", "descartados", "notas_editor"],
    "additionalProperties": False,
}


def _hashtag(evento: str) -> str:
    from .mentions import strip_accents

    return re.sub(r"[^A-Za-z0-9]", "", strip_accents(evento)) or "Desafio4"


def master_prompt(cfg: dict) -> str:
    text = MASTER_PROMPT_PATH.read_text(encoding="utf-8")
    pair = pair_slugs(cfg) + ["", ""]
    values = {
        "evento": cfg["evento"]["nombre"],
        "pareja_a": streamer_name(cfg, pair[0]) if pair[0] else "el streamer principal",
        "pareja_b": streamer_name(cfg, pair[1]) if pair[1] else "su rival",
        "duracion_min": str(int(cfg["edicion"]["duracion_min_min"])),
        "duracion_max": str(int(cfg["edicion"]["duracion_max_min"])),
        "hashtag": _hashtag(cfg["evento"]["nombre"]),
    }
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


GROK_PROMPT_PATH = PROJECT_ROOT / "prompts" / "grok_contexto_x.md"


def grok_prompt(cfg: dict, fecha: str) -> str:
    """Prompt para que Grok (u otro chat con acceso a X) investigue el día; su salida se pega en ClipMax."""
    from datetime import date

    from .timeutil import local_dt, session_window

    start, end = session_window(cfg, date.fromisoformat(fecha))
    zona = cfg["evento"]["zona_horaria"]
    desde = local_dt(start - 3600, cfg).strftime("%H:%M")
    hasta = local_dt(end, cfg).strftime("%H:%M")
    pair = pair_slugs(cfg) + ["", ""]
    others = [f"{s['nombre']} (kick.com/{s['slug']})" for s in cfg["streamers"]
              if s["activo"] and s["slug"] not in pair]
    cuentas = ", ".join(f"@{c}" for c in cfg["x"].get("cuentas") or []) or \
        f"las cuentas oficiales del {cfg['evento']['nombre']} y de su organizador"
    values = {
        "evento": cfg["evento"]["nombre"],
        "ventana": f"el {fecha}, desde las {desde} hasta las {hasta} ({zona}), o hasta ahora si aún no termina",
        "pareja_a": streamer_name(cfg, pair[0]) if pair[0] else "el streamer principal",
        "pareja_b": streamer_name(cfg, pair[1]) if pair[1] else "su rival",
        "streamers": ", ".join(others) or "el resto de participantes",
        "cuentas": cuentas,
    }
    text = GROK_PROMPT_PATH.read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def candidates_path(cfg: dict, fecha: str) -> Path:
    d = session_dir(cfg, fecha) / "claude"
    d.mkdir(exist_ok=True)
    return d / "candidatos.json"


def build_candidates(cfg: dict, db: Database, session: dict) -> list[dict]:
    """Foto de los candidatos que se envían a Claude (se guarda en claude/candidatos.json)."""
    sid = session["id"]
    max_chars = int(cfg["claude"]["max_caracteres_transcripcion"])
    moments = [m for m in db.moments(sid, limit=int(cfg["deteccion"]["candidatos_max"])) if m.get("rank")]
    out = []
    for m in moments:
        segs = db.segments(sid, m["slug"], m["start_ts"], m["end_ts"], "candidato") \
            or db.segments(sid, m["slug"], m["start_ts"], m["end_ts"], "vivo")
        rel = []
        used = 0
        for s in segs:
            line = s["texto"].strip()
            if not line:
                continue
            used += len(line)
            if used > max_chars:
                rel.append([round(s["start_ts"] - m["start_ts"], 1), round(s["end_ts"] - m["start_ts"], 1), "[...]"])
                break
            rel.append([round(max(0.0, s["start_ts"] - m["start_ts"]), 1),
                        round(max(0.0, s["end_ts"] - m["start_ts"]), 1), line])
        chat = db.chat_sample(sid, m["slug"], m["start_ts"] + 10, m["end_ts"], 10)
        out.append({
            "id": m["rank"],
            "moment_id": m["id"],
            "slug": m["slug"],
            "nombre": streamer_name(cfg, m["slug"]),
            "start_ts": m["start_ts"],
            "end_ts": m["end_ts"],
            "duracion": round(m["end_ts"] - m["start_ts"], 1),
            "score": m["score"],
            "por_que": describe_components(cfg, m["componentes"]),
            "mismo_suceso": [],
            "chat": [[c["texto"], c["n"]] for c in chat],
            "transcripcion": rel,
        })
    # "Mismo suceso": otros candidatos (otros streamers) que se solapan en el tiempo.
    for a in out:
        for b in out:
            if a is b or a["slug"] == b["slug"]:
                continue
            overlap = min(a["end_ts"], b["end_ts"]) - max(a["start_ts"], b["start_ts"])
            if overlap >= 20:
                a["mismo_suceso"].append(b["id"])
    return out


def save_candidates(cfg: dict, fecha: str, candidates: list[dict]) -> Path:
    path = candidates_path(cfg, fecha)
    path.write_text(json.dumps(candidates, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_candidates(cfg: dict, fecha: str) -> list[dict]:
    path = candidates_path(cfg, fecha)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def build_day_material(cfg: dict, db: Database, session: dict, candidates: list[dict],
                       x_text: str, x_keywords: list[tuple[str, int]]) -> str:
    """Mensaje del día (Markdown). Es el 'user message' de la API y el paquete manual."""
    ev = cfg["evento"]
    pair = pair_slugs(cfg)
    ed = cfg["edicion"]
    lines = [
        f"# Material del día {session['fecha']} · {ev['nombre']}",
        "",
        f"Horas en hora de Colombia. Duración objetivo del video: {int(ed['duracion_min_min'])}–"
        f"{int(ed['duracion_max_min'])} min. Formato: {ed['formato']}.",
        f"Material disponible: {len(candidates)} candidatos, "
        f"{sum(c['duracion'] for c in candidates) / 60:.1f} min en total.",
        "",
        "## Streamers",
    ]
    for s in cfg["streamers"]:
        if not s["activo"]:
            continue
        tags = []
        if s["slug"] in pair:
            tags.append("pareja principal")
        if s["prioridad"] != 1.0:
            tags.append(f"prioridad {s['prioridad']:g}")
        alias = ", ".join(a for a in s["alias"] if a.lower() not in (s["slug"], s["nombre"].lower()))
        extra = f" · alias: {alias}" if alias else ""
        lines.append(f"- **{s['nombre']}** (kick.com/{s['slug']}){' · ' + ', '.join(tags) if tags else ''}{extra}")

    if ev.get("lore_base", "").strip():
        lines += ["", "## Notas fijas del evento", ev["lore_base"].strip()]

    prev = [p for p in db.previous_sessions(session["fecha"], 3)]
    lore_prev = []
    for p in reversed(prev):
        f = session_dir(cfg, p["fecha"]) / "claude" / "decision.json"
        if f.exists():
            try:
                lore = json.loads(f.read_text(encoding="utf-8")).get("lore_para_manana", "").strip()
            except ValueError:
                lore = ""
            if lore:
                lore_prev.append(f"### {p['fecha']}\n{lore}")
    if lore_prev:
        lines += ["", "## Lore acumulado de días anteriores", *lore_prev]

    lines += ["", "## Qué se comenta hoy en X"]
    if x_keywords:
        lines.append("Temas más repetidos: " + ", ".join(f"{k} ({n})" for k, n in x_keywords[:20]))
    lines.append(x_text.strip() if x_text.strip() else "(sin contexto de X para hoy)")

    # Resumen de menciones por voz del día (del transcriptor en vivo).
    voice = db.signals(session["id"], ["mencion_voz"])
    if voice:
        lines += ["", "## Menciones por voz detectadas en vivo"]
        for s in voice[:40]:
            target = (s["detalle"] or {}).get("target", "")
            txt = (s["detalle"] or {}).get("texto", "")
            lines.append(f"- {fmt_clock(s['ts'], cfg)} {streamer_name(cfg, s['slug'])} → "
                         f"{streamer_name(cfg, target)}: \"{txt}\"")
        if len(voice) > 40:
            lines.append(f"- … y {len(voice) - 40} más")

    lines += ["", f"## Candidatos ({len(candidates)})"]
    for c in candidates:
        lines += [
            "",
            f"### Candidato {c['id']} · {c['nombre']} · {fmt_clock(c['start_ts'], cfg)}–"
            f"{fmt_clock(c['end_ts'], cfg)} ({c['duracion']:.0f} s) · puntuación {c['score']:.2f}",
            f"Por qué se detectó: {c['por_que']}",
        ]
        if c["mismo_suceso"]:
            others = ", ".join(f"candidato {i}" for i in c["mismo_suceso"])
            lines.append(f"Mismo suceso que: {others}")
        if c["chat"]:
            lines.append("Chat: " + " · ".join(f"\"{t[:80]}\"" + (f" ×{n}" if n > 1 else "") for t, n in c["chat"]))
        lines.append("Transcripción (segundos desde el inicio del candidato):")
        if c["transcripcion"]:
            lines += [f"[{a:.1f}–{b:.1f}] {t}" for a, b, t in c["transcripcion"]]
        else:
            lines.append("(sin transcripción)")
    return "\n".join(lines)


def manual_package(cfg: dict, material: str) -> str:
    """Todo en un solo texto para pegar en claude.ai (modo manual, $0 de API)."""
    return (
        master_prompt(cfg)
        + "\n\n---\n\n"
        + material
        + "\n\n---\n\nDevuelve solo el JSON pedido en el formato de respuesta, dentro de un bloque ```json."
    )

