"""Resumen diario en texto: mejores momentos, por qué importan y captions para TikTok.

Se generan dos archivos en data/sesiones/<fecha>/:
- resumen_<fecha>.md   (para copiar/pegar)
- resumen_<fecha>.html (para abrir en el navegador; lo enlaza la interfaz web)
"""

from __future__ import annotations

import html
from pathlib import Path

from .config import session_dir, streamer_name
from .db import Database
from .timeutil import fmt_clock, fmt_duration


def _stats(cfg: dict, db: Database, session: dict) -> list[dict]:
    sid = session["id"]
    rows = []
    for s in cfg["streamers"]:
        total = db.query_one("SELECT COALESCE(SUM(n_msgs),0) AS n FROM chat_buckets WHERE session_id=? AND slug=?",
                             (sid, s["slug"]))["n"]
        peaks = db.query_one("SELECT COUNT(*) AS n FROM signals WHERE session_id=? AND slug=? AND tipo='pico_chat'",
                             (sid, s["slug"]))["n"]
        voice = db.query_one("SELECT COUNT(*) AS n FROM signals WHERE session_id=? AND slug=? AND tipo='mencion_voz'",
                             (sid, s["slug"]))["n"]
        parts = db.list_parts(sid, s["slug"])
        rec = sum((p["duration"] or ((p["ended_at"] or p["started_at"]) - p["started_at"])) for p in parts)
        if total or parts:
            rows.append({"nombre": s["nombre"], "chat": int(total), "picos": int(peaks), "voz": int(voice),
                         "grabado": fmt_duration(rec)})
    return rows


def write_report(cfg: dict, db: Database, session: dict, decision: dict, candidates: list[dict],
                 video: Path | None, tiktok: list[Path]) -> tuple[Path, Path]:
    fecha = session["fecha"]
    folder = session_dir(cfg, fecha)
    by_id = {int(c["id"]): c for c in candidates}
    # export_tiktok_clips nombra cada archivo "NN_titulo.mp4" con NN = índice del mejor momento.
    tiktok_by_idx = {int(p.name[:2]): p for p in tiktok if p.name[:2].isdigit()}
    runs = db.query("SELECT COALESCE(SUM(costo_usd),0) AS c FROM claude_runs WHERE session_id=?", (session["id"],))
    cost = runs[0]["c"] if runs else 0.0
    stats = _stats(cfg, db, session)

    def when(cid: int, a: float, b: float) -> str:
        c = by_id.get(cid)
        if not c:
            return ""
        return f"{fmt_clock(c['start_ts'] + a, cfg)}–{fmt_clock(c['start_ts'] + b, cfg)}"

    # ---------------- Markdown ----------------
    md = [f"# {decision['titulo_video']}", "",
          f"**{cfg['evento']['nombre']} · {fecha}**", ""]
    if video:
        md += [f"Video: `{video.name}`", ""]
    md += ["## Resumen del día", "", decision.get("resumen_del_dia", ""), ""]
    tk = folder / f"resumen_tiktok_{fecha}.mp4"
    if tk.exists():
        md += ["## Resumen para TikTok", "", f"Video vertical: `{tk.name}`", ""]
        if decision.get("caption_resumen_tiktok"):
            md += [f"Caption: {decision['caption_resumen_tiktok']}", ""]
    live = [c for c in db.live_clips(session["id"], ("listo",))]
    if live:
        md += [f"## Clips hechos en vivo ({len(live)})", ""]
        md += [f"- `{Path(c['path']).name}` · {c['titulo']} — {c['caption']} {' '.join(c['hashtags'])}"
               for c in reversed(live)]
        md.append("")
    md += ["## Mejores momentos", ""]
    for i, m in enumerate(decision.get("mejores_momentos", []), 1):
        c = by_id.get(m["candidato_id"], {})
        md += [f"### {i}. {m['titulo']}",
               f"*{c.get('nombre', '?')} · {when(m['candidato_id'], m['inicio'], m['fin'])}*", "",
               f"**Por qué importa:** {m['por_que_importa']}", "", "**Captions para TikTok:**"]
        md += [f"{j}. {cap}" for j, cap in enumerate(m["captions_tiktok"], 1)]
        md += ["", "Hashtags: " + " ".join(m["hashtags"])]
        if i in tiktok_by_idx:
            md.append(f"Clip vertical: `clips_tiktok/{tiktok_by_idx[i].name}`")
        md.append("")
    md += ["## Guion del video", ""]
    for g in decision["guion"]:
        if g["tipo"] == "narracion":
            md.append(f"> 🎙️ {g['texto']}")
        else:
            c = by_id.get(g["candidato_id"], {})
            md.append(f"- 🎬 **{c.get('nombre', '?')}** {when(g['candidato_id'], g['inicio'], g['fin'])} "
                      f"({g['fin'] - g['inicio']:.0f}s, prioridad {g['prioridad']}) — "
                      f"{g.get('titulo_en_pantalla', '')}: {g.get('motivo', '')}")
    md += ["", "## Lore para mañana", "", decision.get("lore_para_manana", ""), ""]
    if stats:
        md += ["## Estadísticas", "", "| Streamer | Grabado | Mensajes de chat | Picos | Menciones por voz |",
               "|---|---|---|---|---|"]
        md += [f"| {r['nombre']} | {r['grabado']} | {r['chat']:,} | {r['picos']} | {r['voz']} |" for r in stats]
        md.append("")
    if decision.get("descartados"):
        md += ["## Descartados", ""]
        md += [f"- Candidato {d['candidato_id']}: {d['motivo']}" for d in decision["descartados"]]
        md.append("")
    notes = [decision.get("notas_editor", "")] + decision.get("advertencias", [])
    notes = [n for n in notes if n]
    if notes:
        md += ["## Notas", ""] + [f"- {n}" for n in notes] + [""]
    md.append(f"_Costo de Claude para este día: ${cost:.3f} · origen de la decisión: {decision.get('origen', '?')}_")
    md_path = folder / f"resumen_{fecha}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    # ---------------- HTML ----------------
    e = html.escape
    cards = []
    for i, m in enumerate(decision.get("mejores_momentos", []), 1):
        c = by_id.get(m["candidato_id"], {})
        caps = "".join(f"<li><span>{e(cap)}</span><button onclick=\"copy(this)\">Copiar</button></li>"
                       for cap in m["captions_tiktok"])
        clip = ""
        if i in tiktok_by_idx:
            rel = f"clips_tiktok/{tiktok_by_idx[i].name}"
            clip = f'<video controls preload="none" src="{e(rel)}"></video>'
        cards.append(f"""<article class="card"><h3>{i}. {e(m['titulo'])}</h3>
<p class="meta">{e(c.get('nombre', '?'))} · {e(when(m['candidato_id'], m['inicio'], m['fin']))}</p>
<p><b>Por qué importa:</b> {e(m['por_que_importa'])}</p>{clip}
<ol class="caps">{caps}</ol><p class="tags">{e(' '.join(m['hashtags']))}</p></article>""")
    guion = []
    for g in decision["guion"]:
        if g["tipo"] == "narracion":
            guion.append(f"<li class='narr'>🎙️ {e(g['texto'])}</li>")
        else:
            c = by_id.get(g["candidato_id"], {})
            guion.append(f"<li>🎬 <b>{e(c.get('nombre', '?'))}</b> {e(when(g['candidato_id'], g['inicio'], g['fin']))} "
                         f"· {e(g.get('titulo_en_pantalla', ''))} — {e(g.get('motivo', ''))}</li>")
    stats_html = "".join(f"<tr><td>{e(r['nombre'])}</td><td>{r['grabado']}</td><td>{r['chat']:,}</td>"
                         f"<td>{r['picos']}</td><td>{r['voz']}</td></tr>" for r in stats)
    video_html = f'<video controls src="{e(video.name)}"></video>' if video else ""
    paragraphs = "".join(f"<p>{e(p)}</p>" for p in decision.get("resumen_del_dia", "").split("\n") if p.strip())
    doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(decision['titulo_video'])}</title>
<style>
:root{{--bg:#0f1115;--fg:#e8e8e8;--mut:#9aa0a6;--card:#181b21;--acc:#53fc18}}
body{{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px;max-width:980px;margin:auto}}
h1{{margin:.2em 0}} h2{{border-bottom:1px solid #2a2e36;padding-bottom:4px;margin-top:32px}}
.meta,.tags{{color:var(--mut)}} .card{{background:var(--card);border-radius:10px;padding:16px;margin:14px 0}}
video{{width:100%;max-height:540px;border-radius:8px;background:#000}}
.caps li{{display:flex;gap:8px;align-items:center;margin:6px 0}} .caps span{{flex:1}}
button{{background:var(--acc);border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-weight:600}}
.narr{{color:var(--mut);font-style:italic}} table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #2a2e36;padding:6px;text-align:left}}
</style></head><body>
<h1>{e(decision['titulo_video'])}</h1><p class="meta">{e(cfg['evento']['nombre'])} · {fecha} · costo Claude ${cost:.3f}</p>
{video_html}
<h2>Resumen del día</h2>{paragraphs}
<h2>Mejores momentos</h2>{''.join(cards)}
<h2>Guion del video</h2><ul>{''.join(guion)}</ul>
<h2>Lore para mañana</h2><p>{e(decision.get('lore_para_manana', ''))}</p>
<h2>Estadísticas</h2><table><tr><th>Streamer</th><th>Grabado</th><th>Chat</th><th>Picos</th><th>Voz</th></tr>{stats_html}</table>
{('<h2>Notas</h2><ul>' + ''.join(f'<li>{e(n)}</li>' for n in notes) + '</ul>') if notes else ''}
<script>function copy(b){{navigator.clipboard.writeText(b.previousElementSibling.textContent);b.textContent='¡Copiado!';setTimeout(()=>b.textContent='Copiar',1500)}}</script>
</body></html>"""
    html_path = folder / f"resumen_{fecha}.html"
    html_path.write_text(doc, encoding="utf-8")
    db.add_output(session["id"], "reporte_md", str(md_path))
    db.add_output(session["id"], "reporte_html", str(html_path))
    return md_path, html_path


def pair_mentions_summary(cfg: dict, db: Database, session: dict) -> list[str]:
    """Líneas legibles con las menciones por voz del día (para el panel web)."""
    out = []
    for s in db.signals(session["id"], ["mencion_voz"]):
        d = s["detalle"] or {}
        out.append(f"{fmt_clock(s['ts'], cfg)} {streamer_name(cfg, s['slug'])} → "
                   f"{streamer_name(cfg, d.get('target', ''))}: {d.get('texto', '')}")
    return out
