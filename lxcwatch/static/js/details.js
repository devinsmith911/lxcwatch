// The container details panel in the drawer.
import { AUTO_POLICIES, Ext, KIND_LABEL, STEP_BADGE, api, duration, fmtWhen, html, jobBadge, post, retired, state, update } from "./core.js";
import { attempt, openDetails, openJob, refresh, runJob, setAutoPolicy, startUpdate } from "./actions.js";
import { openReleaseUpgrade, openSettings, openSnapshot } from "./dialogs.js";

const BAD_BOX = "border-color:color-mix(in srgb, var(--bad) 35%, var(--border));background:color-mix(in srgb, var(--bad) 6%, var(--panel))";
const when = t => new Date(t * 1000).toLocaleString();

// When the container last started, and whether the last job did that.
function Uptime({ c }) {
  if (c.status !== "running") return html`<p class="small muted">Not running (${c.status}).</p>`;
  if (!c.started_at) return null;
  const started = new Date(c.started_at * 1000), j = c.last_job;
  // A restart during (or just after) the job's run counts as caused by it.
  const byJob = j && c.started_at >= j.created_at - 5 && c.started_at <= (j.finished_at || Date.now() / 1000) + 120;
  return html`<p class="small"><span class="muted">Running for</span> <b>${duration(Date.now() / 1000 - c.started_at)}</b>${" "}
    <span class="muted">· started ${started.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>${" "}
    ${byJob && html` <span class="badge info">restarted by the last ${KIND_LABEL[j.kind] || j.kind}</span>`}</p>`;
}

const notesKey = x => `${x.forge}|${x.repo}|${x.installed}|${x.target}`;

async function loadNotes(dr, x) {
  const key = notesKey(x);
  dr.notes[key] = "loading";
  update();
  try {
    const q = new URLSearchParams({ forge: x.forge, repo: x.repo, installed: x.notes_installed || x.installed, target: x.target_tag || x.notes_target || x.target });
    dr.notes[key] = await api(`/api/releases?${q}`);
  } catch (e) {
    dr.notes[key] = { error: e.message };
  }
  update();
}

function ReleaseNotes({ dr, x }) {
  const n = dr.notes[notesKey(x)];
  if (!n) return html`<button class="small" onClick=${() => loadNotes(dr, x)}>What's new since ${x.notes_installed || x.installed}?</button>`;
  if (n === "loading") return html`<div class="whatsnew muted small">Loading releases…</div>`;
  if (n.error) return html`<div class="whatsnew err small">${n.error}</div>`;
  return html`<div class="whatsnew">
    <div class="head small"><b>${n.releases.length} release${n.releases.length === 1 ? "" : "s"} to catch up on${n.complete ? "" : " (older ones not shown)"}</b>${" "}
      ${n.compare_url && html`<${Ext} href=${n.compare_url}>Compare ${n.installed_tag}…${n.target_tag}<//>`}</div>
    ${n.releases.length ? n.releases.map(r => html`<details class="rel" key=${r.tag}>
      <summary><span><b>${r.name}</b>${r.name !== r.tag && html` <span class="muted small">${r.tag}</span>`}</span>${" "}
        <span class="small"><span class="muted">${r.published ? new Date(r.published).toLocaleDateString() : ""}</span> <${Ext} href=${r.url}>open<//></span></summary>
      <pre>${r.notes || "No release notes."}${r.notes_truncated ? "\n…" : ""}</pre></details>`)
      : html`<p class="muted small">No matching releases found.</p>`}</div>`;
}

function Component({ dr, x }) {
  const links = [
    x.source === "apt" && html`<span class="muted small">apt package${x.origin ? ` from ${x.origin}` : ""}</span>`,
    x.release_url && html`<${Ext} href=${x.release_url}>Release ${x.target_tag}<//>`,
    x.repo && html`<${Ext} href=${x.releases_url}>All releases<//><${Ext} href=${x.repo_url}>${x.repo}<//>`,
    x.homepage && !(x.repo_url && x.homepage.startsWith(x.repo_url)) && html`<${Ext} href=${x.homepage}>Project site<//>`,
    x.package_url && html`<${Ext} href=${x.package_url}>${x.origin} package<//>`,
  ].filter(Boolean);
  const badge = x.state === "outdated" ? html`<span class="badge warn">update</span>` : x.state === "current" ? html`<span class="badge ok">current</span>` : html`<span class="badge info">${x.state}</span>`;
  const target = x.target ?? "?";
  return html`<div class="comp-d"><div class="head"><span><b>${x.name}</b> ${badge}</span>${" "}
    <span class="small">installed ${x.installed ?? "?"} · target ${x.release_url ? html`<a href=${x.release_url} target="_blank" rel="noopener">${target}</a>` : target}${
      x.upstream && x.upstream !== x.target && html` · upstream ${x.upstream_url ? html`<a href=${x.upstream_url} target="_blank" rel="noopener">${x.upstream}</a>` : x.upstream}`}</span></div>
    ${links.length > 0 && html`<div class="links">${links}</div>`}
    ${x.state === "outdated" && x.repo && html`<${ReleaseNotes} dr=${dr} x=${x} />`}</div>`;
}

function Stale({ c }) {
  const repos = c.pkg_refresh_errors || [];
  return html`<div class="whatsnew small" style=${BAD_BOX}>
    <b>${repos.length ? `${repos.length === 1 ? "A package source" : `${repos.length} package sources`} couldn't be refreshed` : "The package lists couldn't be refreshed"}</b>, so the container may have updates that aren't listed here.
    ${repos.length > 0 && html`<div class="list" style="margin-top:6px">${repos.map(r => html`<div key=${r.repo}><code>${r.repo}</code><span class="badge bad">${r.reason}</span></div>`)}</div>`}
    ${repos.some(r => r.reason === "signature check failed") && html`<div class="muted" style="margin-top:6px">A failed signature check usually means the repo's signing key changed or uses SHA1, which current Debian rejects. Download the vendor's current key over the keyring file the source's <code>signed-by</code> points to.</div>`}
    ${c.pkg_warnings?.length > 0 && html`<details style="margin-top:6px"><summary class="muted">apt output</summary><pre class="log" style="max-height:200px">${c.pkg_warnings.join("\n")}</pre></details>`}
  </div>`;
}

async function clearFailures(e, vmid) {
  const btn = e.currentTarget;
  if (!confirm(`Dismiss the failure flag in CT ${vmid}? The install log stays.`)) return;
  btn.disabled = true;
  try { await post(`/api/containers/${vmid}/clear-failures`); await refresh(); }
  catch (err) { alert(err.message); }
  btn.disabled = false;
}

function Failure({ c, f }) {
  return html`<div class="whatsnew small" style=${f.superseded ? "" : BAD_BOX}>
    <div class="head"><b>A community-script failed in this container${f.code != null ? ` (exit ${f.code})` : ""}${f.at ? `, ${when(f.at)}` : ""}</b>${" "}
      <button disabled=${!!c.job} onClick=${e => clearFailures(e, c.vmid)}>Dismiss</button></div>
    <div class="muted">${f.superseded && html`<span class="badge ok">resolved</span> A later run succeeded, so this is no longer flagged as a problem. `}Left behind as <code>/root/${f.file}</code>. A successful app update clears it too.</div>
    ${f.errinfo.length > 0 && html`<pre class="log" style="margin-top:8px;max-height:240px">${f.errinfo.join("\n")}</pre>`}
  </div>`;
}

function AutoPanel({ c }) {
  const a = c.auto || { policy: "off" }, g = state.data.auto || {};
  const setPolicy = k => attempt(() => setAutoPolicy([c.vmid], k, c.auto?.rollback || false));
  const setRollback = on => attempt(() => setAutoPolicy([c.vmid], c.auto.policy, on));
  const resume = () => attempt(() => post(`/api/containers/${c.vmid}/auto/resume`).then(refresh));
  let note = null;
  if (a.paused) {
    note = html`<div class="whatsnew small" style=${BAD_BOX}>
      <div class="head"><span><b>Paused</b>: ${a.paused_reason || "an automatic update failed"}. A successful manual update resumes it too.</span>${" "}
        <button onClick=${resume}>Resume</button></div></div>`;
  } else if (a.policy !== "off" && !g.enabled) {
    note = html`<p class="muted small">Automatic updates are turned off in <a href="#" onClick=${e => { e.preventDefault(); openSettings(); }}>Settings</a>, so nothing runs yet.</p>`;
  } else if (a.policy !== "off" && g.next_at) {
    note = html`<p class="muted small">Next window: ${fmtWhen(g.next_at)}${c.lxcwatch_host ? ". lxcwatch runs in this container: its own package and restarts are left to you." : ""}</p>`;
  }
  return html`<div class="autobox">
    <div class="seg wide" role="group" aria-label="Automatic updates">${AUTO_POLICIES.map(([k, short, , desc]) => html`<button key=${k} class=${a.policy === k ? "on" : ""} title=${desc} onClick=${() => setPolicy(k)}>${short}</button>`)}</div>
    ${a.policy !== "off"
      ? html`<label class="check small"><input type="checkbox" checked=${!!a.rollback} onChange=${e => setRollback(e.target.checked)} /> Roll back to the snapshot if an update fails</label>${note}`
      : html`<p class="muted small">${AUTO_POLICIES[0][3]}</p>`}
    ${a.last && html`<p class="small">Last automatic run: <span class="badge ${STEP_BADGE[a.last.status] || ""}">${a.last.status}</span>${a.last.note && html` <span class="muted">${a.last.note}</span>`}</p>`}
  </div>`;
}

function rollBack(vmid, name) {
  if (!confirm(`Roll CT ${vmid} back to ${name}? Everything changed since that snapshot is lost. The container is restarted if it was running.`)) return;
  runJob(`/api/containers/${vmid}/snapshots/${encodeURIComponent(name)}/rollback`, vmid);
}

async function deleteSnapshot(e, vmid, name) {
  const btn = e.currentTarget;
  if (!confirm(`Delete snapshot ${name} of CT ${vmid}?`)) return;
  btn.disabled = true;
  try { await api(`/api/containers/${vmid}/snapshots/${encodeURIComponent(name)}`, { method: "DELETE" }); openDetails(vmid); }
  catch (err) { alert(err.message); btn.disabled = false; }
}

function Snapshots({ dr, c }) {
  const s = dr.snaps;
  if (!s) return html`<p class="muted">Loading…</p>`;
  if (s.status === "rejected") return html`<p class="err">${s.reason.message}</p>`;
  if (!s.value.length) return html`<p class="muted">No snapshots.</p>`;
  return html`<div class="list">${s.value.map(x => html`<div key=${x.name}><span>${x.name} <span class="muted small">${x.snaptime ? when(x.snaptime) : ""} ${x.description || ""}</span></span>${" "}
    <span class="actions"><button disabled=${!!c.job} onClick=${() => rollBack(c.vmid, x.name)}>Roll back</button><button class="danger" disabled=${!!c.job} onClick=${e => deleteSnapshot(e, c.vmid, x.name)}>Delete</button></span></div>`)}</div>`;
}

function Jobs({ dr, c }) {
  const j = dr.jobs;
  if (j?.status !== "fulfilled") return html`<p class="muted">Loading…</p>`;
  if (!j.value.length) return html`<p class="muted">No jobs yet.</p>`;
  return html`<div class="list">${j.value.map(x => html`<div class="jobrow" key=${x.id}><span>${KIND_LABEL[x.kind] || x.kind}${x.auto && html` <span class="badge" title="Started by automatic updates">auto</span>`}<span class="muted small jobwhen">${when(x.created_at)}</span></span>${" "}
    <span class="actions"><span class="badge ${jobBadge(x.status)}">${x.status}${x.rc != null && x.rc !== 0 ? ` (${x.rc})` : ""}</span><button onClick=${() => openJob(x.id, c.vmid)}>Log</button></span></div>`)}</div>`;
}

async function rescan(c) {
  c.scanning = true;
  update();
  await attempt(() => post(`/api/containers/${c.vmid}/scan`));
  refresh();
}

function restart(vmid) {
  if (!confirm(`Restart CT ${vmid}? Its services will be briefly unavailable.`)) return;
  runJob(`/api/containers/${vmid}/restart`, vmid);
}

export function Details({ dr }) {
  const c = state.data.containers.find(x => x.vmid === dr.vmid);
  if (!c) return null;
  const running = c.status === "running", busy = !!c.job;
  const missing = c.components.filter(x => x.missing_version);
  return html`
    <div class="dactions">
      <button disabled=${!running || busy || !c.script} class=${c.outdated ? "primary" : ""} onClick=${() => startUpdate("app", [c.vmid])}>Update app</button>
      <button disabled=${!running || busy || !c.pkgmgr} class=${c.pkg_count ? "primary" : ""} onClick=${() => startUpdate("os", [c.vmid])}>Update packages</button>
      <button disabled=${!running || busy} class=${c.restart_needed ? "primary" : ""} onClick=${() => restart(c.vmid)}>Restart</button>
      <button disabled=${c.scanning || !running} onClick=${() => rescan(c)}>${c.scanning ? "Scanning…" : "Rescan"}</button>
      ${c.job && html`<button data-job=${c.job} onClick=${() => openJob(c.job, c.vmid)}>Live log</button>`}
    </div>
    <${Uptime} c=${c} />
    ${c.error && html`<p class="err">${c.error}</p>`}
    <h3>App versions</h3>
    ${c.script && html`<div class="links">
      ${c.script_page && html`<${Ext} href=${c.script_page}>community-scripts page<//>`}
      <${Ext} href=${c.script_url}>ct/${c.script}.sh<//></div>`}
    ${c.components.length ? html`<div class="list">${c.components.map(x => html`<${Component} key=${x.name} dr=${dr} x=${x} />`)}</div>` : html`<p class="muted">No tracked app versions.</p>`}
    ${retired(c) && html`<div class="whatsnew small" style=${BAD_BOX}>
      <b>The ${c.script} script was retired by community-scripts.</b> ${c.script_state.message || ""}
      <div class="muted" style="margin-top:4px">If it was replaced by another script, its update may ask a question before switching over (Jellyseerr and Overseerr ask to migrate to Seerr). lxcwatch can't answer prompts, so run <code>update</code> once yourself in the container's console, then rescan.</div></div>`}
    ${missing.length > 0 && html`<p class="whatsnew small">${missing.map((x, i) => html`${i > 0 && " "}<b>${x.name}</b> has no version file (<code>${x.version_file}</code>) yet, so lxcwatch can't tell which version is installed. This is normal right after a script migration. The next app update installs the latest release and records it.`)}</p>`}
    ${(c.failures || []).map(f => html`<${Failure} key=${f.file} c=${c} f=${f} />`)}
    ${c.restart_needed && html`<p class="whatsnew small"><b>Restart needed.</b> ${c.stale_procs.length ? html`These are still running code that an update replaced: <code>${c.stale_procs.join(", ")}</code>` : "The container reports that it needs a reboot."}</p>`}
    ${c.release && html`<h3>Release upgrade</h3><div class="whatsnew small"><div class="head">
      <span><b>Debian ${c.release.current} (${c.release.from}) → ${c.release.target} (${c.release.to})</b> is available${c.release.target < c.release.stable ? `, one step towards Debian ${c.release.stable}` : ""}.</span>${" "}
      <button class="primary" disabled=${!running || busy} onClick=${() => openReleaseUpgrade(c)}>Upgrade to Debian ${c.release.target}</button></div>
      <div class="muted" style="margin-top:4px">Runs pre-checks first. Switches the official Debian sources, upgrades every package and restarts the container.</div></div>`}
    <h3>Pending packages (${c.pkg_count})</h3>
    ${c.pkg_stale && html`<${Stale} c=${c} />`}
    ${c.packages.length ? html`<div class="pkgs">${c.packages.map(p => html`<div key=${p.name}>${p.security ? "⚠ " : ""}${p.name} ${p.from || ""} → ${p.to}</div>`)}</div>` : html`<p class="muted">No pending packages.</p>`}
    <h3>Automatic updates</h3>
    <${AutoPanel} c=${c} />
    <div class="sectionhead"><h3>Snapshots</h3><button disabled=${busy} onClick=${() => openSnapshot(c)}>Take snapshot</button></div>
    <${Snapshots} dr=${dr} c=${c} />
    <h3>Job history</h3>
    <${Jobs} dr=${dr} c=${c} />`;
}
