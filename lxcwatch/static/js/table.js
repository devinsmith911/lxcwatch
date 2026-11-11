// The main view: stat boxes, toolbar, batch progress, the container table and paging.
import { AUTO_NAME, AUTO_POLICIES, COLUMNS, DEFAULT_COLUMNS, Ext, KIND_LABEL, STEP_BADGE, ago, api, duration, html, isUnknown, notScript, post, problem, retired, rowState, state, store, until, update, useLayoutEffect, useRef } from "./core.js";
import { attempt, openDetails, openDialog, openJob, refresh, setAutoPolicy, startUpdate } from "./actions.js";
import { openUpdateAll } from "./dialogs.js";

// How each column sorts: the value to compare, and which way the first click
// goes. null means "nothing to show" and always sorts last, in either direction.
const AUTO_RANK = { security: 1, packages: 2, app: 3 };
const lower = s => s ? s.toLowerCase() : null;
const SORTS = {
  container: [c => c.vmid, "asc"],
  node: [c => lower(c.node), "asc"],
  tags: [c => lower(c.tags.join(" ")), "asc"],
  app: [c => lower(c.components[0]?.name || c.script), "asc"],
  // Security updates count for more than other pending packages.
  packages: [c => c.pkgmgr ? c.pkg_security * 1e6 + c.pkg_count : null, "desc"],
  auto: [c => AUTO_RANK[c.auto?.policy] ?? null, "desc"],
  uptime: [c => c.status === "running" && c.started_at ? -c.started_at : null, "desc"],
  // Oldest first: never scanned counts as the oldest of all.
  scanned: [c => c.scanned_at || 0, "asc"],
};

// The sort in effect: none if its column is hidden.
const activeSort = () => state.sort && SORTS[state.sort.key] && state.columns.includes(state.sort.key) ? state.sort : null;

function sorted(rows) {
  const s = activeSort();
  if (!s) return rows;
  const value = SORTS[s.key][0], sign = s.dir === "desc" ? -1 : 1;
  return rows.map(c => [value(c), c]).sort(([a, x], [b, y]) => {
    if (a === null || b === null) return a === b ? x.vmid - y.vmid : a === null ? 1 : -1;
    const d = typeof a === "string" ? a.localeCompare(b, undefined, { numeric: true }) : a - b;
    return d ? d * sign : x.vmid - y.vmid;
  }).map(([, c]) => c);
}

function setSort(sort) {
  state.sort = sort;
  store("sort", sort);
  state.page = 1;
  update();
}

// Header clicks: first direction, then the other, then back to the usual order.
function cycleSort(key) {
  const first = SORTS[key][1], cur = activeSort();
  if (cur?.key !== key) setSort({ key, dir: first });
  else if (cur.dir === first) setSort({ key, dir: first === "asc" ? "desc" : "asc" });
  else setSort(null);
}

function visible() {
  const q = state.q.toLowerCase();
  return sorted(state.data.containers.filter(c => {
    if (state.hideStopped && c.status !== "running") return false;
    if (state.hideNonScript && notScript(c)) return false;
    if (state.filter === "unknown" && !isUnknown(c)) return false;
    if (state.filter === "app" && !c.outdated) return false;
    if (state.filter === "packages" && !c.pkg_count) return false;
    if (state.filter === "security" && !c.pkg_security) return false;
    if (state.filter === "problems" && !problem(c)) return false;
    if (q && ![c.vmid, c.name, c.node, ...c.tags, c.script].join(" ").toLowerCase().includes(q)) return false;
    return true;
  }));
}

// The rows on the current page, keeping the page number in range.
function pageOf(rows) {
  const size = Number(state.pageSize) || rows.length || 1;
  const pages = Math.max(1, Math.ceil(rows.length / size));
  state.page = Math.min(Math.max(1, state.page), pages);
  const start = (state.page - 1) * size;
  return { start, pages, shown: rows.slice(start, start + size) };
}

function setFilter(f) {
  state.filter = state.filter === f ? "all" : f;
  state.page = 1;
  update();
}

function Stats() {
  const strip = useRef();
  // On mobile the boxes scroll sideways: bring the selected one into view
  // (scrollLeft only, so the page itself doesn't jump).
  useLayoutEffect(() => {
    const el = strip.current, active = el.querySelector(".stat.active");
    if (active && (active.offsetLeft < el.scrollLeft || active.offsetLeft + active.offsetWidth > el.scrollLeft + el.clientWidth)) {
      el.scrollTo({ left: active.offsetLeft - 12, behavior: "smooth" });
    }
  }, [state.filter]);
  const cs = state.data?.containers;
  const running = cs?.filter(c => c.status === "running").length;
  // Each box filters the table; clicking the active one shows everything again.
  const stats = cs ? [
    ["all", "Containers", cs.length, "--accent", `${running} running · ${cs.length - running} stopped`],
    ["app", "App updates", cs.filter(c => c.outdated).length, "--warn"],
    ["packages", "Package updates", cs.filter(c => c.pkg_count).length, "--info"],
    ["security", "Security", cs.filter(c => c.pkg_security).length, "--bad"],
    ["problems", "Problems", cs.filter(problem).length, "--bad"],
    ["unknown", "Unknown version", cs.filter(isUnknown).length, "--idle"],
  ] : [];
  return html`<div class="stats" id="stats" ref=${strip}>${stats.map(([f, k, v, color, sub]) => {
    const live = v || f === "all";
    return html`<button key=${f} class="stat${live ? "" : " zero"}${state.filter === f ? " active" : ""}" aria-pressed=${String(state.filter === f)}
      style="--c: var(${live ? color : "--idle"})" onClick=${() => setFilter(f)}><b>${v}</b><span class="label">${k}</span>${sub && html`<span class="sub">${sub}</span>`}</button>`;
  })}</div>`;
}

function ScanInfo() {
  const s = state.data?.scan;
  let info = "Loading…";
  if (state.error) info = html`<span class="err">${state.error}</span>`;
  else if (s) {
    const next = s.next_at ? ` · next ${until(s.next_at)}` : s.interval_minutes > 0 ? "" : " · scheduled scans off";
    const scanning = s.progress?.total ? `Scanning ${s.progress.done} of ${s.progress.total}…` : "Scanning…";
    info = html`${s.running ? scanning : `${s.last ? `Last scan ${ago(s.last.at)}` : "Not scanned yet"}${next}`}${s.error && html` · <span class="badge bad">${s.error}</span>`}`;
  }
  return html`<span class="scaninfo" id="scaninfo">${info}</span>`;
}

function ScanProgress() {
  const s = state.data?.scan, p = s?.running && s.progress;
  if (!p?.total) return null;
  return html`<div class="bar scanprogress" id="scanprogress" role="progressbar" aria-label="Scan progress" aria-valuemin="0" aria-valuemax=${p.total} aria-valuenow=${p.done}>
    <div style="width:${Math.round(p.done / p.total * 100)}%"></div></div>`;
}

async function scanNow() {
  await attempt(() => post("/api/scan"));
  refresh();
}

function toggleSetting(key) {
  return e => { state[key] = e.target.checked; store(key, state[key]); state.page = 1; update(); };
}

function setColumns(keys) {
  state.columns = COLUMNS.map(([k]) => k).filter(k => keys.includes(k));
  store("columns", state.columns);
  update();
}

// Which table columns to show, remembered in this browser.
function ColumnPicker() {
  const on = state.columns;
  const custom = on.join() !== DEFAULT_COLUMNS.join();
  return html`<details class="colpick" id="columns">
    <summary class="toggle chip${custom ? " custom" : ""}" title="Choose which columns to show">Columns</summary>
    <div class="pop">
      ${COLUMNS.map(([k, label]) => html`<label key=${k} class="check"><input type="checkbox" checked=${on.includes(k)}
        onChange=${e => setColumns(e.target.checked ? [...on, k] : on.filter(x => x !== k))} /> ${label}</label>`)}
      <hr />
      <button disabled=${!custom} onClick=${() => setColumns(DEFAULT_COLUMNS)}>Reset to default</button>
    </div>
  </details>`;
}

const colLabel = k => COLUMNS.find(([c]) => c === k)[1];

// Phones have no column headings to click, so they get a select instead.
function SortPicker() {
  const s = activeSort();
  return html`<span class="sortpick only-mobile">
    <select id="sortsel" aria-label="Sort by" onChange=${e => setSort(e.target.value ? { key: e.target.value, dir: SORTS[e.target.value][1] } : null)}>
      <option value="" selected=${!s}>Sort: default</option>
      ${state.columns.map(k => html`<option key=${k} value=${k} selected=${s?.key === k}>Sort: ${colLabel(k)}</option>`)}
    </select>
    ${s && html`<button type="button" class="chip" title="Reverse the order" aria-label=${s.dir === "asc" ? "Ascending, reverse" : "Descending, reverse"}
      onClick=${() => setSort({ ...s, dir: s.dir === "asc" ? "desc" : "asc" })}>${s.dir === "asc" ? "↑" : "↓"}</button>`}
  </span>`;
}

function SortHeader({ k }) {
  const s = activeSort(), on = s?.key === k;
  return html`<th class="c-${k}" aria-sort=${on ? (s.dir === "asc" ? "ascending" : "descending") : undefined}>
    <button type="button" class="sorth${on ? " on" : ""}" onClick=${() => cycleSort(k)}>${colLabel(k)}<span class="sortarrow" aria-hidden="true">${on ? (s.dir === "asc" ? "↑" : "↓") : "↕"}</span></button></th>`;
}

const Toolbar = () => html`<div class="toolbar">
  <input type="search" id="q" placeholder="Filter by name, ID, node, tag" value=${state.q} onInput=${e => { state.q = e.target.value; state.page = 1; update(); }} />
  <label class="toggle chip"><input type="checkbox" id="hidestopped" checked=${state.hideStopped} onChange=${toggleSetting("hideStopped")} /> Hide stopped</label>
  <label class="toggle chip" title="Containers that weren't installed with community-scripts (no update command)"><input type="checkbox" id="hidenonscript" checked=${state.hideNonScript} onChange=${toggleSetting("hideNonScript")} /> Hide non-scripts</label>
  <${ColumnPicker} />
  <${SortPicker} />
  <span class="spacer"></span>
  <div class="scanbar">
    <${ScanInfo} />
    <${ScanProgress} />
    <button id="scanbtn" disabled=${!!state.data?.scan.running} onClick=${scanNow}>Scan now</button>
    <button id="updateall" class="primary" onClick=${openUpdateAll}>Update all</button>
  </div>
</div>`;

async function stopBatch() {
  state.batchStopping = true;
  update();
  await attempt(() => post("/api/batch/stop"));
}

async function dismissBatch() {
  await post("/api/batch/dismiss").catch(() => {});
  refresh();
}

const STEP_ICON = { pending: "○", running: "◐", success: "✓", failed: "✗", skipped: "–" };
const stepKind = s => s.kind === "app" ? "app" : "packages";

function Batch() {
  const b = state.data?.batch;
  if (!b) return html`<div id="batchbox"></div>`;
  if (b.status !== "running") state.batchStopping = false;
  const count = st => b.steps.filter(s => s.status === st).length;
  const done = b.steps.filter(s => !["pending", "running"].includes(s.status)).length;
  const cur = b.steps.find(s => s.status === "running");
  const title = b.status === "running"
    ? `${b.auto ? "Automatic updates" : "Updating all"} · ${done} of ${b.steps.length} done${cur ? ` · now ${cur.vmid} ${cur.name} (${cur.kind === "app" ? "app" : "packages"})` : ""}`
    : `${b.auto ? "Automatic updates" : "Update all"} ${b.status === "done" ? "finished" : b.status === "stopped" ? "stopped" : "failed"} · ${count("success")} succeeded${count("failed") ? `, ${count("failed")} failed` : ""}${count("skipped") ? `, ${count("skipped")} skipped` : ""}`;
  return html`<div id="batchbox"><div class="batch">
    <div class="head"><b>${title}</b>${" "}
      ${b.status === "running"
        ? html`<button id="batchstop" disabled=${!!state.batchStopping} onClick=${stopBatch}>${state.batchStopping ? "Stopping after current…" : "Stop"}</button>`
        : html`<button id="batchdismiss" onClick=${dismissBatch}>Dismiss</button>`}</div>
    <div class="bar"><div style="width:${Math.round(done / b.steps.length * 100)}%"></div></div>
    <div class="steps">${b.steps.map(s => html`<span key=${`${s.vmid}-${s.kind}`} class="badge ${STEP_BADGE[s.status]}" data-job=${s.job || undefined}
      title=${s.job ? "Show log" : s.note || undefined} onClick=${s.job ? () => openJob(s.job, s.vmid) : undefined}>${STEP_ICON[s.status]} ${s.vmid} ${s.name} · ${stepKind(s)}${s.note && s.status !== "running" ? ` — ${s.note}` : ""}</span>`)}</div>
  </div></div>`;
}

// Apps installed with apt: a small tag naming where the package came from.
const AptTag = ({ x }) => x.source === "apt" && html` <span class="badge" title="Installed with apt${x.origin ? ` from ${x.origin}` : ""}">apt</span>`;

function Component({ x }) {
  const name = html`${x.name}<${AptTag} x=${x} />`;
  if (x.state === "outdated") {
    return html`<div class="comp" title=${x.repo}>${name} ${x.installed} <span class="arrow">→ ${x.release_url
      ? html`<a href=${x.release_url} target="_blank" rel="noopener" title="Release notes for ${x.target_tag}">${x.target}</a>` : x.target}</span>${x.pinned && html` <span class="badge">pinned</span>`}${x.release_url && html` <${Ext} class="small ext" href=${x.release_url}>notes<//>`}</div>`;
  }
  if (x.state === "current") {
    const behind = x.pinned && x.upstream && x.upstream !== x.target;
    return html`<div class="comp">${name} ${x.installed} <span class="badge ok">current</span>${behind && html` <span class="badge" title="community-scripts pins ${x.target}; upstream has ${x.upstream}">pinned</span>`}</div>`;
  }
  if (x.missing_version) {
    return html`<div class="comp" title="No ${x.version_file} yet; the next app update records it">${name} <span class="badge info">version unknown</span>${x.target && html` <span class="muted small">latest ${x.target}</span>`}</div>`;
  }
  return html`<div class="comp">${name} ${x.installed ?? "?"} <span class="badge info">${x.state === "untracked" ? "untracked" : "unknown"}</span></div>`;
}

function AppCell({ c }) {
  if (c.status !== "running" && !c.components.length) return html`<span class="muted dash">—</span>`;
  if (!c.script && !c.components.length) return html`<span class="badge info">not a community-script</span>`;
  const pending = c.components.some(x => x.pending), gone = retired(c);
  return html`${c.components.map(x => html`<${Component} key=${x.name} x=${x} />`)}
    ${pending && html`<span class="badge bad">last update did not finish</span>`}
    ${gone && html`<span class="badge bad" title=${c.script_state.message || undefined}>script retired</span>`}
    ${!c.components.length && !pending && !gone && html`<span class="muted small">${c.script} · updates via packages</span>`}`;
}

function PackagesCell({ c }) {
  if (!c.pkgmgr) return html`<span class="muted dash">—</span>`;
  const extras = html`${c.pkg_stale && html` <span class="badge bad" title="Package lists couldn't be refreshed${c.pkg_refresh_errors?.length ? `: ${c.pkg_refresh_errors.map(e => e.repo).join(", ")}` : ""}. See Details.">lists stale</span>`}
    ${c.restart_needed && html` <span class="badge warn" title=${c.stale_procs.length ? `Still running replaced code: ${c.stale_procs.join(", ")}` : "The container asks for a reboot"}>restart needed</span>`}
    ${c.release && html` <span class="badge info" title="Release upgrade available, see Details">Debian ${c.release.target} available</span>`}`;
  if (!c.pkg_count) return html`${!c.pkg_stale && html`<span class="badge ok">up to date</span>`}${extras}`;
  return html`<span class="badge warn">${c.pkg_count} pending</span>${c.pkg_security > 0 && html` <span class="badge bad">${c.pkg_security} security</span>`}${extras}`;
}

const AUTO_SHORT = Object.fromEntries(AUTO_POLICIES.map(([k, short]) => [k, short]));

function AutoCell({ c }) {
  if (!c.auto || c.auto.policy === "off") return null;
  // No column headings on mobile, so say what the badge is about there.
  const what = html`<span class="only-mobile">auto${" "}</span>`;
  if (c.auto.paused) return html`<span class="badge warn" title="Automatic updates paused: ${c.auto.paused_reason || ""}">${what}paused</span>`;
  const on = state.data.auto?.enabled;
  return html`<span class="badge${on ? "" : " muted"}" title="Automatic updates: ${AUTO_NAME[c.auto.policy]}${on ? "" : " (turned off under Automatic updates in the menu)"}">${what}${AUTO_SHORT[c.auto.policy]}</span>`;
}

// The optional cells, by column key. Empty ones collapse on mobile.
const CELLS = {
  container: (c, rs) => html`<td class="c-container"><div class="name"><span class="dot" title=${rs}></span>${c.vmid} · ${c.name}${c.status !== "running" && html` <span class="badge">${c.status}</span>`}</div>
    ${c.os && html`<div class="small muted ctinfo">${c.os}</div>`}
    ${c.error && html`<div class="err small">${c.error}</div>`}</td>`,
  node: c => html`<td class="c-node"><span class="badge node">${c.node}</span></td>`,
  tags: c => html`<td class="c-tags">${c.tags.length > 0 && html`<div class="tags">${c.tags.map(t => html`<span class="badge" key=${t}>${t}</span>`)}</div>`}</td>`,
  app: c => html`<td class="c-app" data-label="App:"><${AppCell} c=${c} />${c.upstream_errors.length > 0 && html`<div class="err small">${c.upstream_errors.join("; ")}</div>`}</td>`,
  packages: c => html`<td class="c-packages" data-label="OS:"><${PackagesCell} c=${c} /></td>`,
  auto: c => html`<td class="c-auto"><${AutoCell} c=${c} /></td>`,
  uptime: c => html`<td class="c-uptime small muted" title=${c.started_at ? "Running since " + new Date(c.started_at * 1000).toLocaleString() : undefined}>${c.status === "running" && c.started_at ? html`<span class="only-mobile">${"up\u00a0"}</span>${duration(Date.now() / 1000 - c.started_at)}` : html`<span class="dash">—</span>`}</td>`,
  scanned: c => html`<td class="c-scanned small muted">${c.scanning ? "scanning…" : ago(c.scanned_at)}</td>`,
};

function toggleSelected(id, on) {
  on ? state.selected.add(id) : state.selected.delete(id);
  update();
}

function Row({ c }) {
  const running = c.status === "running", busy = !!c.job, picked = state.selected.has(c.vmid), rs = rowState(c);
  // Update buttons only appear when there's something to update; Details always
  // has them for a manual run.
  return html`<tr class="r-${rs}${picked ? " selected" : ""}">
    <td class="c-select"><input type="checkbox" checked=${picked} aria-label="Select ${c.vmid}" onChange=${e => toggleSelected(c.vmid, e.target.checked)} /></td>
    ${state.columns.map(k => CELLS[k](c, rs))}
    <td class="c-actions"><div class="actions">
      ${busy ? html`<span class="badge warn">updating…</span>` : c.last_job?.status === "failed" && html`<span class="badge bad">last ${KIND_LABEL[c.last_job.kind] || c.last_job.kind} failed</span>`}
      ${running && !busy && c.script && (c.outdated || c.components.some(x => x.missing_version)) ? html`<button class="primary" onClick=${() => startUpdate("app", [c.vmid])}>Update app</button>` : null}
      ${running && !busy && c.pkgmgr && c.pkg_count ? html`<button onClick=${() => startUpdate("os", [c.vmid])}>Update packages</button>` : null}
      <button onClick=${() => openDetails(c.vmid)}>Details</button>
    </div></td></tr>`;
}

function selectPage(on) {
  for (const c of pageOf(visible()).shown) on ? state.selected.add(c.vmid) : state.selected.delete(c.vmid);
  update();
}

function Table() {
  const shown = state.data ? pageOf(visible()).shown : [];
  return html`<table>
    <thead><tr>
      <th style="width:28px"><input type="checkbox" id="selall" aria-label="Select all" checked=${shown.length > 0 && shown.every(c => state.selected.has(c.vmid))} onChange=${e => selectPage(e.target.checked)} /></th>
      ${state.columns.map(k => html`<${SortHeader} key=${k} k=${k} />`)}<th></th>
    </tr></thead>
    <tbody id="rows">${!state.data ? null : shown.length ? shown.map(c => html`<${Row} key=${c.vmid} c=${c} />`)
      : html`<tr class="empty"><td colspan=${state.columns.length + 2} class="muted">Nothing here.</td></tr>`}</tbody>
  </table>`;
}

function goPage(delta) {
  state.page += delta;
  update();
  scrollTo({ top: 0, behavior: "smooth" });
}

function Pager() {
  const d = state.data, rows = d ? visible() : [];
  const { start, shown, pages } = pageOf(rows);
  const total = d?.containers.length;
  const info = !d ? "" : rows.length ? `Showing ${start + 1}–${start + shown.length} of ${rows.length}${rows.length < total ? ` (${total - rows.length} filtered out)` : ""}` : `0 of ${total} containers match`;
  return html`<div class="pager">
    <span class="muted small" id="pageinfo">${info}</span>
    <div class="actions">
      <label class="muted small" for="pagesize">Per page</label>
      <select id="pagesize" onChange=${e => { state.pageSize = Number(e.target.value); store("pageSize", state.pageSize); state.page = 1; update(); }}>${[[10, "10"], [25, "25"], [50, "50"], [0, "All"]].map(([v, label]) => html`<option key=${v} value=${v} selected=${Number(state.pageSize) === v}>${label}</option>`)}</select>
      <button id="prev" disabled=${!!d && state.page <= 1} onClick=${() => goPage(-1)}>‹ Prev</button>
      <span class="small" id="pagenum">${d ? `Page ${state.page} of ${pages}` : ""}</span>
      <button id="next" disabled=${!!d && state.page >= pages} onClick=${() => goPage(1)}>Next ›</button>
    </div>
  </div>`;
}

function bulkUpdate(kind) {
  const ids = state.data.containers.filter(c => state.selected.has(c.vmid) && c.status === "running" && !c.job && (kind === "os" ? c.pkgmgr : c.script)).map(c => c.vmid);
  if (!ids.length) return alert("None of the selected containers can run that update.");
  startUpdate(kind, ids);
}

async function bulkAuto() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const cs = state.data.containers.filter(c => state.selected.has(c.vmid));
  const r = await openDialog("auto", { count: ids.length, cs });
  if (r) await attempt(() => setAutoPolicy(ids, r.policy, r.rollback));
}

function Bulk() {
  const n = state.selected.size;
  return html`<div class="bulk${n > 0 ? " show" : ""}" id="bulk">
    <span id="bulkcount">${state.data ? `${n} selected` : ""}</span>
    <label class="check"><input type="checkbox" id="bulksnap" checked=${state.bulkSnapshot} onChange=${e => { state.bulkSnapshot = e.target.checked; update(); }} /> Snapshot first</label>
    <button id="bulkapp" class="primary" onClick=${() => bulkUpdate("app")}>Update apps</button>
    <button id="bulkos" onClick=${() => bulkUpdate("os")}>Update packages</button>
    <button id="bulkauto" onClick=${bulkAuto}>Automatic updates</button>
    <button id="bulkclear" onClick=${() => { state.selected.clear(); update(); }}>Clear</button>
  </div>`;
}

export const Dashboard = () => html`<main>
  <${Stats} />
  <${Toolbar} />
  <${Batch} />
  <${Table} />
  <${Pager} />
</main>
<${Bulk} />`;
