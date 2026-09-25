// Utilidades compartidas por las páginas de ClipMax.
async function api(url, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(url, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { data = { ok: false, error: `HTTP ${r.status}` }; }
  if (!r.ok || data.ok === false) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function toast(msg, ms = 3500) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function mb(bytes) { return bytes ? (bytes / 1048576).toFixed(0) + " MB" : "—"; }

async function copyText(text) {
  await navigator.clipboard.writeText(text);
  toast("Copiado al portapapeles");
}

// --- Clips para TikTok armados en vivo (Panel y página de la sesión) ---
const LIVE_CLIPS = {};
function clipText(c) { return [c.caption, (c.hashtags || []).join(" ")].filter(Boolean).join("\n\n"); }
function renderLiveClips(el, clips) {
  const key = JSON.stringify(clips);
  if (el.dataset.key === key) return;          // sin cambios: no redibujar (el Panel refresca cada 3 s)
  el.dataset.key = key;
  if (!clips.length) { el.innerHTML = '<div class="mut small">Todavía no hay clips. Salen solos a medida que el chat explota.</div>'; return; }
  clips.forEach(c => { LIVE_CLIPS[c.id] = c; });
  el.innerHTML = clips.map(c => {
    if (c.estado === "procesando") return `<div class="card clip"><div class="small"><span class="badge procesando">editando</span> ${esc(c.nombre)} · ${c.hora}</div></div>`;
    if (c.estado === "error") return `<div class="card clip"><div class="small"><span class="badge error">error</span> ${esc(c.nombre)} · ${c.hora}</div><div class="small mut">${esc(c.nota)}</div></div>`;
    if (c.estado === "descartado") return `<div class="card clip"><div class="small"><span class="badge sin_datos">descartado</span> ${esc(c.nombre)} · ${c.hora}</div><div class="small mut">${esc(c.nota)}</div>
      <div class="row" style="margin-top:6px"><button class="sec small" onclick="publicarIgual(${c.id}, this)">Publicar igual</button></div></div>`;
    return `<div class="card clip${c.subido ? " subido" : ""}">
      ${c.thumb ? `<a href="${c.url}" target="_blank"><img src="${c.thumb}" alt=""></a>` : ""}
      <div><b>${esc(c.titulo)}</b></div>
      <div class="small mut">${esc(c.nombre)} · ${c.hora} · ${c.duracion ? Math.round(c.duracion) + " s" : ""} · ${c.origen === "claude" ? "Claude" : "auto"}</div>
      <div class="small caption">${esc(clipText(c))}</div>
      <div class="row" style="margin-top:6px">
        <a class="btn" href="${c.url}" download>Descargar</a>
        <button class="sec small" onclick="copiarCaption(${c.id})">Copiar caption</button>
        <label class="row small" style="margin:0"><input type="checkbox" ${c.subido ? "checked" : ""} onchange="marcarSubido(${c.id}, this.checked)"> subido</label>
      </div></div>`;
  }).join("");
}
async function copiarCaption(id) { await copyText(clipText(LIVE_CLIPS[id])); toast("Caption copiado"); }
async function publicarIgual(id, btn) {
  btn.disabled = true;
  try { await api(`/api/clips-vivo/${id}/publicar`, {}); toast("Armando el clip: aparece en 1-2 minutos"); }
  catch (e) { toast(e.message); btn.disabled = false; }
}
async function marcarSubido(id, val) { try { await api(`/api/clips-vivo/${id}/subido`, { subido: val }); } catch (e) { toast(e.message); } }
async function pedirClip(momentId) {
  try { await api("/api/clips-vivo/crear", { moment_id: momentId }); toast("Clip en cola: sale apenas ese momento termine de grabarse"); }
  catch (e) { toast(e.message); }
}
