// ---- Activity: recent jobs across all containers ----
const JOB_FILTERS = { all: "All", running: "Running", success: "Succeeded", failed: "Failed" };

function openActivity() {
  if (state.es) { state.es.close(); state.es = null; }
  state.drawer = { type: "activity", filter: state.activityFilter || "all", jobs: null };
  openDrawer("Activity", "Recent jobs across all containers");
  renderActivity();
  loadActivity();
}

async function loadActivity() {
  const dr = state.drawer;
  try { dr.jobs = await api("/api/jobs?limit=200"); dr.error = null; }
  catch (e) { dr.error = e.message; }
  if (state.drawer === dr) renderActivity();
}

function renderActivity() {
  const dr = state.drawer, names = Object.fromEntries((state.data?.containers || []).map(c => [c.vmid, c.name]));
  const all = dr.jobs || [];
  const match = j => dr.filter === "all" || (dr.filter === "running" ? ["queued", "running"].includes(j.status) : j.status === dr.filter);
  const count = f => f === "all" ? all.length : all.filter(j => f === "running" ? ["queued", "running"].includes(j.status) : j.status === f).length;
  const rows = all.filter(match);
  const chips = Object.entries(JOB_FILTERS).map(([f, label]) =>
    `<label class="toggle chip"><input type="radio" name="jobfilter" data-jobfilter="${f}" ${dr.filter === f ? "checked" : ""}> ${label} <span class="muted">${count(f)}</span></label>`).join("");
  const now = Date.now() / 1000;
  const list = rows.map(j => {
    const ended = j.finished_at || (["queued", "running"].includes(j.status) ? now : null);
    const status = j.status === "success" ? "ok" : j.status === "failed" ? "bad" : "warn";
    return `<div class="row">
      <span class="when">${new Date(j.created_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>
      <span class="what"><a data-ctlink="${j.vmid}">${j.vmid} · ${esc(names[j.vmid] || "(removed)")}</a>
        <span class="meta"> · ${esc(KIND_LABEL[j.kind] || j.kind)}${j.auto ? " (automatic)" : ""}${ended ? ` · ${duration(ended - j.created_at)}` : ""}</span></span>
      <span class="actions"><span class="badge ${status}">${esc(j.status)}${j.rc != null && j.rc !== 0 ? ` (${j.rc})` : ""}</span><button data-joblog="${esc(j.id)}" data-jobvmid="${j.vmid}">Log</button></span>
    </div>`;
  }).join("");
  $("#dbody").innerHTML = `<div class="actions" style="margin-bottom:10px">${chips}</div>
    ${dr.error ? `<p class="err">${esc(dr.error)}</p>` : !dr.jobs ? `<p class="muted">Loading…</p>`
      : rows.length ? `<div class="list activity">${list}</div><p class="muted small">Showing the latest ${all.length} jobs.</p>`
      : `<p class="muted">No ${dr.filter === "all" ? "" : JOB_FILTERS[dr.filter].toLowerCase() + " "}jobs yet.</p>`}`;
}
