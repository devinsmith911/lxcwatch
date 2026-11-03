const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function stored(key, fallback) {
  try { const v = localStorage.getItem(`lxcwatch.${key}`); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function store(key, value) {
  try { localStorage.setItem(`lxcwatch.${key}`, JSON.stringify(value)); } catch {}
}
const state = {
  data: null, filter: "all", q: "", selected: new Set(), drawer: null, es: null,
  page: 1, pageSize: stored("pageSize", 10), hideStopped: stored("hideStopped", false), hideNonScript: stored("hideNonScript", false),
};

function toLogin() {
  location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Requested-With": "lxcwatch", "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (r.status === 401) { toLogin(); throw new Error("Signed out"); }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || r.statusText);
  return body;
}

function duration(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`;
}

// When the container last started, and whether the last job did that.
function uptimeHtml(c) {
  if (c.status !== "running") return `<p class="small muted">Not running (${esc(c.status)}).</p>`;
  if (!c.started_at) return "";
  const started = new Date(c.started_at * 1000);
  const j = c.last_job;
  // A restart during (or just after) the job's run counts as caused by it.
  const byJob = j && c.started_at >= j.created_at - 5 && c.started_at <= (j.finished_at || Date.now() / 1000) + 120;
  return `<p class="small"><span class="muted">Running for</span> <b>${duration(Date.now() / 1000 - c.started_at)}</b>
    <span class="muted">· started ${started.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>
    ${byJob ? ` <span class="badge info">restarted by the last ${esc(KIND_LABEL[j.kind] || j.kind)}</span>` : ""}</p>`;
}

function until(ts) {
  const s = ts - Date.now() / 1000;
  if (s < 90) return "any moment";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  if (s < 86400) return `in ${Math.round(s / 3600)}h`;
  return `in ${Math.round(s / 86400)}d`;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
