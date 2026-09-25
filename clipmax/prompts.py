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

from . import effects, xcontext
from .config import PROJECT_ROOT, pair_slugs, session_dir, streamer_name
from .db import Database
from .detector import describe_components
from .mentions import normalize
from .timeutil import fmt_clock

MASTER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "prompt_maestro.md"

# Esquema de salida (structured outputs). Todas las propiedades son obligatorias y
# additionalProperties=false, como exige la API; los rangos se validan en brain.py.
_GUION_ITEM = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["narracion", "clip", "gancho"]},
        "texto": {"type": "string"},
        "candidato_id": {"type": "integer"},
        "inicio": {"type": "number"},
        "fin": {"type": "number"},
        "titulo_en_pantalla": {"type": "string"},
        "prioridad": {"type": "integer"},
        "motivo": {"type": "string"},
        "momento_clave": {"type": "number"},
        "efecto_sonido": {"type": "string"},
        "pantalla_dividida_con": {"type": "integer"},
        # Estilo Eufonía (prompts/estilo_eufonia.md); en el clásico van vacíos salvo emociones/zoom_texto.
        "bloque": {"type": "string", "enum": ["", "gancho", "premisa", "cuerpo", "subida", "pausa", "climax",
                                              "desenlace", "cierre"]},
        "emociones": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"}, "tipo": {"type": "string"}, "texto": {"type": "string"}},
            "required": ["t", "tipo", "texto"], "additionalProperties": False}},
        "zoom_texto": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"},
                           "zona": {"type": "string", "enum": ["chat", "juego", "centro", "arriba"]},
                           "texto": {"type": "string"}},
            "required": ["t", "zona", "texto"], "additionalProperties": False}},
        "facecam_completo": {"type": "array", "items": {
            "type": "object",
            "properties": {"inicio": {"type": "number"}, "fin": {"type": "number"}},
            "required": ["inicio", "fin"], "additionalProperties": False}},
        "rotulo": {"type": "string"},
        "conservar_silencios": {"type": "boolean"},
        "zoom_final": {"type": "boolean"},
        "repeticiones": {"type": "integer"},
    },
    "required": ["tipo", "texto", "candidato_id", "inicio", "fin", "titulo_en_pantalla", "prioridad", "motivo",
                 "momento_clave", "efecto_sonido", "pantalla_dividida_con", "bloque", "emociones", "zoom_texto",
                 "facecam_completo", "rotulo", "conservar_silencios", "zoom_final", "repeticiones"],
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
        # Resumen vertical para TikTok (máximo ~4 min), narrado en off (prompts/prompt_maestro.md).
        "resumen_tiktok": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidato_id": {"type": "integer"},
                    "inicio": {"type": "number"},
                    "fin": {"type": "number"},
                    "texto_en_pantalla": {"type": "string"},
                    "momento_clave": {"type": "number"},
                    "narracion": {"type": "string"},
                    "tipo_evento": {"type": "string", "enum": ["muerte", "explosion", "anuncio", "pique", "alianza",
                                                               "traicion", "logro", "otro"]},
                    "contador": {"type": "string"},
                    "suma": {"type": "integer"},
                    "palabra_clave": {"type": "string"},
                    "cita_inicio": {"type": "number"},
                    "cita_fin": {"type": "number"},
                    "prioridad": {"type": "integer"},
                },
                "required": ["candidato_id", "inicio", "fin", "texto_en_pantalla", "momento_clave", "narracion",
                             "tipo_evento", "contador", "suma", "palabra_clave", "cita_inicio", "cita_fin",
                             "prioridad"],
                "additionalProperties": False,
            },
        },
        "tiktok_intro": {"type": "string"},
        "tiktok_cierre": {"type": "string"},
        "tiktok_contadores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "etiqueta": {"type": "string"},
                    "icono": {"type": "string", "enum": ["calavera", "corazon", "espada", "estrella", "diamante",
                                                         "casa", "rayo", "trofeo"]},
                    "inicial": {"type": "integer"},
                },
                "required": ["id", "etiqueta", "icono", "inicial"],
                "additionalProperties": False,
            },
        },
        "caption_resumen_tiktok": {"type": "string"},
    },
    "required": ["titulo_video", "resumen_del_dia", "lore_para_manana", "guion",
                 "mejores_momentos", "descartados", "notas_editor", "resumen_tiktok", "tiktok_intro",
                 "tiktok_cierre", "tiktok_contadores", "caption_resumen_tiktok"],
    "additionalProperties": False,
}


def _hashtag(evento: str) -> str:
    from .mentions import strip_accents

    return re.sub(r"[^A-Za-z0-9]", "", strip_accents(evento)) or "Desafio4"


def master_prompt(cfg: dict) -> str:
    text = MASTER_PROMPT_PATH.read_text(encoding="utf-8")
    estilo = cfg["edicion"].get("estilo", "eufonia")
    guide = PROJECT_ROOT / "prompts" / f"estilo_{estilo}.md"
    text = text.replace("{{guia_estilo}}", guide.read_text(encoding="utf-8").strip() if guide.exists() else "")
    pair = pair_slugs(cfg) + ["", ""]
    values = {
        "evento": cfg["evento"]["nombre"],
        "pareja_a": streamer_name(cfg, pair[0]) if pair[0] else "el streamer principal",
        "pareja_b": streamer_name(cfg, pair[1]) if pair[1] else "su rival",
        "duracion_min": str(int(cfg["edicion"]["duracion_min_min"])),
        "duracion_max": str(int(cfg["edicion"]["duracion_max_min"])),
        "hashtag": _hashtag(cfg["evento"]["nombre"]),
        "efectos": ", ".join(effects.sfx_names(cfg)),
        "max_efectos": str(int(cfg["edicion"].get("sfx_max_por_video", 10))),
        "tiktok_max_s": str(int(cfg["edicion"].get("resumen_tiktok_max_s", 240))),
    }
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


CLIP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "clip_vivo.md"

# Respuesta de Claude para un clip en vivo (salida estructurada).
CLIP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["publicar", "motivo", "inicio", "fin", "momento_clave", "titulo", "caption", "hashtags",
                 "efecto_sonido", "emociones", "zoom_texto"],
    "properties": {
        "publicar": {"type": "boolean"},
        "motivo": {"type": "string"},
        "inicio": {"type": "number"},
        "fin": {"type": "number"},
        "momento_clave": {"type": "number"},
        "titulo": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "efecto_sonido": {"type": "string"},
        "emociones": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"}, "tipo": {"type": "string"}, "texto": {"type": "string"}},
            "required": ["t", "tipo", "texto"], "additionalProperties": False}},
        "zoom_texto": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"},
                           "zona": {"type": "string", "enum": ["chat", "juego", "centro", "arriba"]},
                           "texto": {"type": "string"}},
            "required": ["t", "zona", "texto"], "additionalProperties": False}},
    },
}


def clip_prompt(cfg: dict) -> str:
    """System prompt de los clips en vivo (prompts/clip_vivo.md con la configuración)."""
    pair = pair_slugs(cfg) + ["", ""]
    cv = cfg["clips_vivo"]
    values = {
        "evento": cfg["evento"]["nombre"],
        "pareja_a": streamer_name(cfg, pair[0]) if pair[0] else "la pareja principal",
        "pareja_b": streamer_name(cfg, pair[1]) if pair[1] else "su rival",
        "hashtag": _hashtag(cfg["evento"]["nombre"]),
        "efectos": ", ".join(effects.sfx_names(cfg)),
        "dur_min": str(cv["duracion_min_s"]),
        "dur_max": str(cv["duracion_max_s"]),
    }
    text = CLIP_PROMPT_PATH.read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


GROK_PROMPT_PATH = PROJECT_ROOT / "prompts" / "grok_contexto_x.md"


def grok_prompt(cfg: dict, fecha: str, db: Database | None = None) -> str:
    """Prompt para que Grok (u otro chat con acceso a X) investigue el día; su salida se pega en ClipMax.

    Con `db`, le pasa los hilos de días anteriores para que busque cómo siguen.
    """
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
        "historia": history_for_grok(previous_days(cfg, db, fecha)) if db else "",
    }
    values["historia"] = values["historia"] or "(no hay días anteriores registrados; es el primer día)"
    text = GROK_PROMPT_PATH.read_text(encoding="utf-8")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


# -- memoria entre días ------------------------------------------------------------
def _previous_lore(cfg: dict, fecha: str) -> str:
    f = session_dir(cfg, fecha) / "claude" / "decision.json"
    if not f.exists():
        return ""
    try:
        return str(json.loads(f.read_text(encoding="utf-8")).get("lore_para_manana") or "").strip()
    except ValueError:
        return ""


def previous_days(cfg: dict, db: Database, fecha: str, days: int = 3, x_chars: int = 1500) -> list[dict]:
    """Memoria de los días anteriores (del más viejo al más reciente) que tienen algo que contar.

    Cada día: {"fecha", "lore" (lo que Claude escribió en lore_para_manana), "x" (resumen del
    contexto de X pegado ese día), "x_texto" (el contexto completo, para cruzar temas)}.
    """
    out = []
    # Se mira más atrás por si hay días vacíos (pruebas, domingos, días sin datos) en medio.
    for p in db.previous_sessions(fecha, days * 4):
        x_full = (p.get("x_contexto") or "").strip()
        if not x_full:
            x_full = "\n\n".join(post["texto"] for post in db.x_posts(p["id"]))
        day = {"fecha": p["fecha"], "lore": _previous_lore(cfg, p["fecha"]),
               "x": xcontext.digest(x_full, x_chars), "x_texto": x_full}
        if day["lore"] or day["x"]:
            out.append(day)
            if len(out) >= days:
                break
    return out[::-1]


def history_for_grok(history: list[dict], max_chars: int = 3500) -> str:
    """Hilos de días anteriores en pocas líneas, para que Grok busque cómo siguen."""
    blocks = []
    for day in history:
        secs = xcontext.sections(day["x_texto"])
        body = day["lore"] or "\n".join(secs[k] for k in ("PIQUES", "CONTINUACIONES") if secs.get(k)) \
            or day["x"]
        if body:
            blocks.append(f"{day['fecha']}:\n{body.strip()}")
    text = "\n\n".join(blocks)
    while len(text) > max_chars and len(blocks) > 1:   # sobra: se quita el día más viejo
        blocks.pop(0)
        text = "\n\n".join(blocks)
    return text[:max_chars]


def _everyday_words(cfg: dict) -> set[str]:
    """Palabras que salen todos los días y no indican continuidad (evento y nombres de streamers)."""
    words = set(normalize(cfg["evento"]["nombre"]).split()) | {"minecraft", "kick", "stream", "directo", "hoy"}
    for s in cfg["streamers"]:
        for name in [s["slug"], s["nombre"], *s["alias"]]:
            words |= set(normalize(name).split())
    return words


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

    history = previous_days(cfg, db, session["fecha"])
    if history:
        lines += ["", "## Historia de días anteriores (memoria)",
                  "Del más viejo al más reciente. «Lore» es lo que tú escribiste ese día en `lore_para_manana`; "
                  "«En X» es lo que se comentaba ese día. Úsalo para callbacks: qué pique sigue, qué promesa "
                  "se cumplió, quién cambió de bando."]
        for day in history:
            lines += ["", f"### {day['fecha']}"]
            if day["lore"]:
                lines.append(f"**Lore:** {day['lore']}")
            if day["x"]:
                lines.append(f"**En X:**\n{day['x']}")

    lines += ["", "## Qué se comenta hoy en X"]
    if x_keywords:
        lines.append("Temas más repetidos: " + ", ".join(f"{k} ({n})" for k, n in x_keywords[:20]))
        recurring = xcontext.recurring_topics(x_keywords, {d["fecha"]: d["x_texto"] for d in history},
                                              ignore=_everyday_words(cfg))
        if recurring:
            lines.append("Temas de hoy que ya venían de días anteriores: " + ", ".join(
                f"{k} ({', '.join(f[5:] for f in fechas)})" for k, fechas in recurring))
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

