// The side drawer and what it can show: job logs and recent activity. details.js has the third.
import { Icon, KIND_LABEL, duration, html, jobBadge, state, update, useEffect, useRef, useState } from "./core.js";
import { closeDrawer, openActivity, openDetails, openJob, refresh } from "./actions.js";
import { Details } from "./details.js";

// ---- Job log: streamed straight into the <pre>, since logs can be long ----
function JobLog({ dr }) {
  const [status, setStatus] = useState({ text: "connecting…", cls: "warn" });
  const pre = useRef();
  useEffect(() => {
    const es = new EventSource(`/api/jobs/${encodeURIComponent(dr.id)}/stream`);
    dr.stop = () => es.close();
    es.onopen = () => setStatus(s => ({ ...s, text: "running" }));
    es.onmessage = ev => {
      const el = pre.current;
      const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
      el.textContent += JSON.parse(ev.data) + "\n";
      if (stick) el.scrollTop = el.scrollHeight;
    };
    es.addEventListener("done", ev => {
      const r = JSON.parse(ev.data);
      setStatus({ text: r.status + (r.rc != null && r.rc !== 0 ? ` (exit ${r.rc})` : ""), cls: r.status === "success" ? "ok" : "bad" });
      es.close();
      refresh();
    });
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setStatus(s => ({ ...s, text: "disconnected" })); };
    return () => es.close();
  }, []);
  return html`<div class="actions" style="margin-bottom:10px"><span id="jstatus" class="badge ${status.cls}">${status.text}</span>
      ${dr.from === "activity" ? html`<button onClick=${openActivity}>Back to activity</button>`
        : dr.vmid ? html`<button onClick=${() => openDetails(dr.vmid)}>Back to container</button>` : null}</div>
    <pre class="log" id="jlog" ref=${pre}></pre>`;
}

// ---- Activity: recent jobs across all containers ----
const JOB_FILTERS = { all: "All", running: "Running", success: "Succeeded", failed: "Failed" };
const isActive = j => ["queued", "running"].includes(j.status);
const jobMatches = (j, f) => f === "all" || (f === "running" ? isActive(j) : j.status === f);

function setJobFilter(dr, f) {
  dr.filter = state.activityFilter = f;
  update();
}

function JobRow({ j, name }) {
  const ended = j.finished_at || (isActive(j) ? Date.now() / 1000 : null);
  return html`<div class="row">
    <span class="when">${new Date(j.created_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>
    <span class="what"><a onClick=${() => openDetails(j.vmid)}>${j.vmid} · ${name || "(removed)"}</a>${" "}
      <span class="meta">· ${KIND_LABEL[j.kind] || j.kind}${j.auto ? " (automatic)" : ""}${ended ? ` · ${duration(ended - j.created_at)}` : ""}</span></span>
    <span class="actions"><span class="badge ${jobBadge(j.status)}">${j.status}${j.rc != null && j.rc !== 0 ? ` (${j.rc})` : ""}</span><button onClick=${() => openJob(j.id, j.vmid, "activity")}>Log</button></span>
  </div>`;
}

function Activity({ dr }) {
  const names = Object.fromEntries((state.data?.containers || []).map(c => [c.vmid, c.name]));
  const all = dr.jobs || [];
  const rows = all.filter(j => jobMatches(j, dr.filter));
  return html`<div class="actions" style="margin-bottom:10px">${Object.entries(JOB_FILTERS).map(([f, label]) => html`
      <label class="toggle chip" key=${f}><input type="radio" name="jobfilter" checked=${dr.filter === f} onChange=${() => setJobFilter(dr, f)} /> ${label} <span class="muted">${all.filter(j => jobMatches(j, f)).length}</span></label>`)}</div>
    ${dr.error ? html`<p class="err">${dr.error}</p>` : !dr.jobs ? html`<p class="muted">Loading…</p>`
      : rows.length ? html`<div class="list activity">${rows.map(j => html`<${JobRow} key=${j.id} j=${j} name=${names[j.vmid]} />`)}</div><p class="muted small">Showing the latest ${all.length} jobs.</p>`
      : html`<p class="muted">No ${dr.filter === "all" ? "" : JOB_FILTERS[dr.filter].toLowerCase() + " "}jobs yet.</p>`}`;
}

const VIEWS = { ct: Details, activity: Activity, job: JobLog };

export function Drawer() {
  // Once closed, keep showing the last content while the drawer slides away.
  const last = useRef({});
  const dr = state.drawer;
  if (dr) {
    const View = VIEWS[dr.type];
    last.current = { title: dr.title, sub: dr.sub, body: html`<${View} key=${dr.key} dr=${dr} />` };
  }
  const { title, sub, body } = last.current;
  return html`<div class="scrim${dr ? " show" : ""}" id="scrim" title="Close" onClick=${closeDrawer}></div>
    <aside class="drawer${dr ? " open" : ""}" id="drawer">
      <header>
        <div><h1 id="dtitle">${title}</h1><div class="muted small" id="dsub">${sub}</div></div>
        <button id="dclose" class="xbtn" aria-label="Close" title="Close (Esc)" onClick=${closeDrawer}><${Icon}><path d="M6 6l12 12M18 6L6 18"/><//></button>
      </header>
      <div class="body" id="dbody">${body}</div>
    </aside>`;
}
