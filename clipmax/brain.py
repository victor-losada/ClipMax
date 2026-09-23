"""Claude como cerebro editorial.

Modo "api": una sola llamada por día a la API de Anthropic con
  - system = prompts/prompt_maestro.md
  - user   = material del día (candidatos + transcripciones + contexto de X)
  - salida estructurada (JSON schema) -> nunca hay que "adivinar" el JSON
  - control de presupuesto mensual: antes de llamar se cuentan los tokens y se
    estima el costo; si el mes se pasaría del tope, NO se llama y se genera el
    paquete para modo manual.
Modo "manual" ($0 de API): se exporta un .md para pegar en claude.ai y luego se
  importa la respuesta JSON desde la interfaz web.

Ambos modos terminan igual: decision.json validado en data/sesiones/<fecha>/claude/.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .config import session_dir
from .db import Database
from .prompts import OUTPUT_SCHEMA, master_prompt

log = logging.getLogger(__name__)

# Modelos donde activamos el reintento automático en otro modelo si hay rechazo.
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Salida esperada para el cálculo previo de costo (guion + reporte + razonamiento).
EXPECTED_OUTPUT_TOKENS = 14000


class BudgetExceeded(RuntimeError):
    pass


class ClaudeError(RuntimeError):
    pass


def claude_dir(cfg: dict, fecha: str) -> Path:
    d = session_dir(cfg, fecha) / "claude"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Costos
# ---------------------------------------------------------------------------

def prices(cfg: dict, model: str) -> tuple[float, float]:
    table = cfg["claude"]["precios"]
    if model in table:
        return float(table[model][0]), float(table[model][1])
    for key, val in table.items():  # p. ej. un id con sufijo de otra plataforma
        if model.startswith(key):
            return float(val[0]), float(val[1])
    log.warning("Sin precio configurado para %s; asumo tarifa de Opus", model)
    return 5.0, 25.0


def cost_from_usage(cfg: dict, model: str, usage) -> float:
    p_in, p_out = prices(cfg, model)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    c_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    searches = 0
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        searches = getattr(stu, "web_search_requests", 0) or 0
    usd = (inp * p_in + c_w * p_in * 1.25 + c_r * p_in * 0.1 + out * p_out) / 1_000_000
    usd += searches * float(cfg["claude"]["precio_busqueda_web_usd"])
    return round(usd, 5)


def budget_status(cfg: dict, db: Database) -> dict:
    spent = db.month_spend()
    budget = float(cfg["claude"]["presupuesto_mensual_usd"])
    return {"gastado": round(spent, 4), "presupuesto": budget, "restante": round(budget - spent, 4)}


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

def _client():
    import anthropic

    # Toma ANTHROPIC_API_KEY del entorno (.env se carga al arrancar ClipMax).
    try:
        return anthropic.Anthropic(max_retries=3, timeout=900)
    except anthropic.AnthropicError as exc:
        raise ClaudeError("Falta ANTHROPIC_API_KEY en el archivo .env (o usa claude.modo: manual)") from exc


def _model_params(cfg: dict, model: str) -> dict:
    """Parámetros según la familia del modelo (Haiku 4.5 no acepta effort ni thinking adaptativo)."""
    params: dict = {}
    output_config: dict = {}
    if not model.startswith("claude-haiku"):
        params["thinking"] = {"type": "adaptive"}
        output_config["effort"] = cfg["claude"]["esfuerzo"]
    if output_config:
        params["output_config"] = output_config
    return params


def _text_of(message) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", "") == "text")


def _stream_call(client, params: dict, use_fallback: bool):
    """Llamada en streaming (evita timeouts con max_tokens altos) con fallback opcional."""
    import anthropic

    if use_fallback:
        try:
            with client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default") as stream:
                return stream.get_final_message()
        except anthropic.BadRequestError as exc:
            if "fallback" not in str(exc).lower():
                raise
            log.info("El modelo no acepta fallbacks; reintento sin ellos")
    with client.beta.messages.stream(**params) as stream:
        return stream.get_final_message()


def decide_api(cfg: dict, db: Database, session: dict, material: str) -> dict:
    """Pide a Claude la decisión editorial del día. Devuelve el dict crudo de Claude."""
    import anthropic

    model = cfg["claude"]["modelo"]
    system = master_prompt(cfg)
    messages = [{"role": "user", "content": material}]
    client = _client()
    folder = claude_dir(cfg, session["fecha"])

    # 1) Presupuesto: contar tokens (gratis) y estimar el peor caso razonable.
    try:
        counted = client.messages.count_tokens(model=model, system=system, messages=messages).input_tokens
    except anthropic.AuthenticationError as exc:
        raise ClaudeError("ANTHROPIC_API_KEY inválida o ausente (revisa el archivo .env)") from exc
    except anthropic.NotFoundError as exc:
        raise ClaudeError(f"El modelo {model!r} no existe o tu cuenta no tiene acceso") from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"No pude contar tokens ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("Sin conexión con la API de Anthropic") from exc
    except TypeError as exc:  # el SDK lanza TypeError si no encuentra ninguna credencial
        if "authentication" not in str(exc).lower():
            raise
        raise ClaudeError("Falta ANTHROPIC_API_KEY en el archivo .env (o usa claude.modo: manual)") from exc
    p_in, p_out = prices(cfg, model)
    estimate = (counted * p_in + EXPECTED_OUTPUT_TOKENS * p_out) / 1_000_000
    status = budget_status(cfg, db)
    log.info("Claude: %d tokens de entrada, costo estimado $%.3f (gastado este mes $%.2f de $%.2f)",
             counted, estimate, status["gastado"], status["presupuesto"])
    if status["gastado"] + estimate > status["presupuesto"]:
        raise BudgetExceeded(
            f"La llamada costaría ~${estimate:.2f} y este mes ya van ${status['gastado']:.2f} "
            f"de ${status['presupuesto']:.2f}. Usa el modo manual (pegar en claude.ai) o sube el tope."
        )

    params = {
        "model": model,
        "max_tokens": int(cfg["claude"]["max_tokens"]),
        "system": system,
        "messages": messages,
        **_model_params(cfg, model),
    }
    params.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    (folder / "request.json").write_text(
        json.dumps({k: v for k, v in params.items() if k != "system"}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    use_fallback = bool(cfg["claude"]["fallback_por_rechazo"]) and model in FALLBACK_MODELS
    t0 = time.time()
    try:
        msg = _stream_call(client, params, use_fallback)
    except anthropic.AuthenticationError as exc:
        raise ClaudeError("ANTHROPIC_API_KEY inválida o ausente (revisa el archivo .env)") from exc
    except anthropic.RateLimitError as exc:
        raise ClaudeError("Límite de uso de la API alcanzado; reintenta en unos minutos") from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"Error de la API ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("Sin conexión con la API de Anthropic") from exc

    served_by = getattr(msg, "model", model) or model
    cost = cost_from_usage(cfg, served_by, msg.usage)
    db.add_claude_run(
        session_id=session["id"], tipo="decision", modelo=served_by,
        input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
        cache_read=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        cache_write=getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
        costo_usd=cost, ok=int(msg.stop_reason == "end_turn"),
        nota=f"stop={msg.stop_reason} {time.time() - t0:.0f}s",
    )
    (folder / "respuesta_raw.json").write_text(msg.to_json(), encoding="utf-8")
    log.info("Claude respondió (%s, %s) en %.0fs: %d in / %d out tokens, $%.3f",
             served_by, msg.stop_reason, time.time() - t0,
             msg.usage.input_tokens, msg.usage.output_tokens, cost)

    if msg.stop_reason == "refusal":
        details = getattr(msg, "stop_details", None)
        raise ClaudeError(f"Claude rechazó la solicitud ({getattr(details, 'category', None)}). "
                          "Revisa el contexto de X pegado o usa el modo manual.")
    if msg.stop_reason == "max_tokens":
        raise ClaudeError("La respuesta se cortó por max_tokens; sube claude.max_tokens en la configuración")
    text = _text_of(msg)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ClaudeError(f"Claude no devolvió JSON válido: {text[:300]}") from exc


def research_x_web(cfg: dict, db: Database, session: dict) -> str:
    """x.modo = claude_web: Claude busca en la web qué se comenta hoy del evento."""
    import anthropic

    model = cfg["claude"]["modelo"]
    max_uses = int(cfg["x"]["busquedas_web_max"])
    status = budget_status(cfg, db)
    p_in, p_out = prices(cfg, model)
    estimate = (60000 * p_in + 3000 * p_out) / 1_000_000 + max_uses * float(cfg["claude"]["precio_busqueda_web_usd"])
    if status["gastado"] + estimate > status["presupuesto"]:
        raise BudgetExceeded("Presupuesto insuficiente para la búsqueda web de contexto")
    from .config import pair_slugs, streamer_name

    names = ", ".join(streamer_name(cfg, s) for s in pair_slugs(cfg))
    prompt = (
        f"Hoy es {session['fecha']}. Busca qué se está comentando hoy en X (Twitter), TikTok y medios "
        f"sobre el evento de Minecraft '{cfg['evento']['nombre']}' en Kick, especialmente sobre {names}. "
        "Resume en viñetas: las historias o piques del día, quién dijo qué de quién, alianzas o traiciones, "
        "y los momentos que la gente está clipeando. Incluye la fuente de cada punto. "
        "Si no encuentras nada de hoy, dilo claramente en vez de rellenar."
    )
    tool_type = "web_search_20250305" if model.startswith("claude-haiku") else "web_search_20260209"
    client = _client()
    messages: list = [{"role": "user", "content": prompt}]
    total_cost = 0.0
    text_parts: list[str] = []
    for _ in range(4):  # la búsqueda puede pausar el turno (pause_turn); se continúa
        try:
            with client.messages.stream(
                model=model, max_tokens=8000, messages=messages,
                tools=[{"type": tool_type, "name": "web_search", "max_uses": max_uses}],
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.APIError as exc:
            raise ClaudeError(f"Búsqueda web falló: {exc}") from exc
        cost = cost_from_usage(cfg, msg.model or model, msg.usage)
        total_cost += cost
        db.add_claude_run(session_id=session["id"], tipo="investigacion_x", modelo=msg.model or model,
                          input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
                          web_searches=getattr(getattr(msg.usage, "server_tool_use", None), "web_search_requests", 0) or 0,
                          costo_usd=cost, ok=1, nota=f"stop={msg.stop_reason}")
        text_parts.append(_text_of(msg))
        if msg.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": msg.content}]
    summary = "\n".join(t for t in text_parts if t).strip()
    log.info("Contexto web de X obtenido ($%.3f)", total_cost)
    if summary:
        db.add_x_posts(session["id"], [{"ext_id": f"claude_web_{session['fecha']}", "autor": "Resumen web (Claude)",
                                        "texto": summary[:6000], "url": None, "created_at": time.time(),
                                        "likes": 0}], "claude_web")
    return summary


# ---------------------------------------------------------------------------
# Modo manual (claude.ai)
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """Saca el JSON de una respuesta pegada (con o sin bloque ```json)."""
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else text[text.find("{"): text.rfind("}") + 1]
    if not candidate.strip():
        raise ClaudeError("No encontré un objeto JSON en la respuesta pegada")
    try:
        return json.loads(candidate)
    except ValueError as exc:
        raise ClaudeError(f"El JSON pegado no es válido: {exc}") from exc


# ---------------------------------------------------------------------------
# Validación (aplica a ambos modos)
# ---------------------------------------------------------------------------

def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def validate_decision(cfg: dict, raw: dict, candidates: list[dict]) -> tuple[dict, list[str]]:
    """Normaliza la decisión, recorta tiempos al rango de cada candidato y ajusta la duración."""
    warnings: list[str] = []
    by_id = {int(c["id"]): c for c in candidates}
    guion = []
    for i, item in enumerate(raw.get("guion") or []):
        tipo = str(item.get("tipo", "")).lower()
        if tipo == "narracion":
            texto = str(item.get("texto") or "").strip()
            if texto:
                guion.append({"tipo": "narracion", "texto": texto, "motivo": str(item.get("motivo") or "")})
            continue
        if tipo != "clip":
            warnings.append(f"guion[{i}]: tipo desconocido {tipo!r}")
            continue
        cid = int(_num(item.get("candidato_id"), -1))
        cand = by_id.get(cid)
        if not cand:
            warnings.append(f"guion[{i}]: candidato {cid} no existe; se omite")
            continue
        a = max(0.0, _num(item.get("inicio")))
        b = min(cand["duracion"], _num(item.get("fin"), cand["duracion"]))
        if b - a < 2.0:
            warnings.append(f"guion[{i}]: corte de {b - a:.1f}s demasiado corto; se omite")
            continue
        guion.append({
            "tipo": "clip", "candidato_id": cid, "inicio": round(a, 2), "fin": round(b, 2),
            "titulo_en_pantalla": str(item.get("titulo_en_pantalla") or "").strip()[:60],
            "prioridad": int(min(5, max(1, _num(item.get("prioridad"), 3)))),
            "motivo": str(item.get("motivo") or ""),
        })
    if not any(g["tipo"] == "clip" for g in guion):
        raise ClaudeError("La decisión no tiene ningún clip válido")

    # Duración: si se pasa del máximo, se quitan clips de prioridad baja (y su narración previa).
    max_s = float(cfg["edicion"]["duracion_max_min"]) * 60
    min_s = float(cfg["edicion"]["duracion_min_min"]) * 60

    def total() -> float:
        return sum(g["fin"] - g["inicio"] for g in guion if g["tipo"] == "clip")

    while total() > max_s:
        clips = [(g["prioridad"], -(g["fin"] - g["inicio"]), idx) for idx, g in enumerate(guion) if g["tipo"] == "clip"]
        if len(clips) <= 1:
            break
        _, _, idx = min(clips)
        dropped = guion.pop(idx)
        prev_is_narr = idx > 0 and guion[idx - 1]["tipo"] == "narracion"
        next_is_clip = idx < len(guion) and guion[idx]["tipo"] == "clip"
        # La narración que presentaba solo ese clip ya no tiene sentido (el gancho inicial se respeta).
        if prev_is_narr and not next_is_clip and idx - 1 > 0:
            guion.pop(idx - 1)
        warnings.append(f"Se quitó el clip del candidato {dropped['candidato_id']} (prioridad "
                        f"{dropped['prioridad']}) para no pasar de {max_s / 60:.0f} min")
    if total() < min_s:
        warnings.append(f"Los clips suman {total() / 60:.1f} min (< {min_s / 60:.0f} min objetivo). "
                        "Tras quitar silencios puede quedar aún más corto.")

    mejores = []
    for m in raw.get("mejores_momentos") or []:
        cid = int(_num(m.get("candidato_id"), -1))
        cand = by_id.get(cid)
        if not cand:
            warnings.append(f"mejor momento con candidato inexistente {cid}; se omite")
            continue
        a = max(0.0, _num(m.get("inicio")))
        b = min(cand["duracion"], _num(m.get("fin"), cand["duracion"]))
        if b - a < 2:
            a, b = 0.0, cand["duracion"]
        mejores.append({
            "candidato_id": cid, "inicio": round(a, 2), "fin": round(b, 2),
            "titulo": str(m.get("titulo") or "").strip(),
            "por_que_importa": str(m.get("por_que_importa") or "").strip(),
            "captions_tiktok": [str(c).strip() for c in (m.get("captions_tiktok") or []) if str(c).strip()][:5],
            "hashtags": [("#" + str(h).lstrip("#")).replace(" ", "") for h in (m.get("hashtags") or []) if str(h).strip()][:8],
        })
    decision = {
        "titulo_video": str(raw.get("titulo_video") or f"{cfg['evento']['nombre']} · resumen").strip(),
        "resumen_del_dia": str(raw.get("resumen_del_dia") or "").strip(),
        "lore_para_manana": str(raw.get("lore_para_manana") or "").strip(),
        "guion": guion,
        "mejores_momentos": mejores,
        "descartados": [
            {"candidato_id": int(_num(d.get("candidato_id"), -1)), "motivo": str(d.get("motivo") or "")}
            for d in raw.get("descartados") or [] if isinstance(d, dict)
        ],
        "notas_editor": str(raw.get("notas_editor") or "").strip(),
        "advertencias": warnings,
    }
    return decision, warnings


def save_decision(cfg: dict, db: Database, session: dict, decision: dict, origen: str) -> Path:
    path = claude_dir(cfg, session["fecha"]) / "decision.json"
    decision = {**decision, "origen": origen, "guardado": time.time()}
    path.write_text(json.dumps(decision, ensure_ascii=False, indent=1), encoding="utf-8")
    db.add_output(session["id"], "decision", str(path), {"origen": origen})
    return path


def load_decision(cfg: dict, fecha: str) -> dict | None:
    path = claude_dir(cfg, fecha) / "decision.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
