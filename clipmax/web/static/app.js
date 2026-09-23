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
