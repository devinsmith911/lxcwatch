function openDrawer(title, sub) {
  $("#dtitle").textContent = title;
  $("#dsub").textContent = sub || "";
  $("#drawer").classList.add("open");
  $("#scrim").classList.add("show");
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#scrim").classList.remove("show");
  state.drawer = null;
  if (state.es) { state.es.close(); state.es = null; }
}

async function openDetails(vmid) {
  if (state.es) { state.es.close(); state.es = null; }
  state.drawer = { type: "ct", vmid, snaps: null, jobs: null, notes: {}, open: new Set() };
  renderDetails(true);
  const [snaps, jobs] = await Promise.allSettled([api(`/api/containers/${vmid}/snapshots`), api(`/api/jobs?vmid=${vmid}`)]);
  if (state.drawer?.vmid !== vmid) return;
  state.drawer.snaps = snaps;
  state.drawer.jobs = jobs;
  renderDetails(false);
}

function renderDetails(first) {
  const dr = state.drawer;
  const c = state.data.containers.find(x => x.vmid === dr.vmid);
  if (!c) return closeDrawer();
  if (first) openDrawer(`${c.vmid} · ${c.name}`, `${c.node} · ${c.os || ""}${c.script ? ` · ct/${c.script}.sh` : ""}`);
  const comps = c.components.length ? `<div class="list">${c.components.map(x => compDetail(x, dr)).join("")}</div>`
    : `<p class="muted">No tracked app versions.</p>`;
  const scriptLinks = c.script ? `<div class="links">
      ${c.script_page ? `<a class="ext" href="${esc(c.script_page)}" target="_blank" rel="noopener">community-scripts page</a>` : ""}
      <a class="ext" href="${esc(c.script_url)}" target="_blank" rel="noopener">ct/${esc(c.script)}.sh</a></div>` : "";
  const pk = c.packages.length ? `<div class="pkgs">${c.packages.map(p => `<div>${p.security ? "⚠ " : ""}${esc(p.name)} ${esc(p.from || "")} → ${esc(p.to)}</div>`).join("")}</div>` : `<p class="muted">No pending packages.</p>`;
  let snaps = `<p class="muted">Loading…</p>`;
  if (dr.snaps?.status === "rejected") snaps = `<p class="err">${esc(dr.snaps.reason.message)}</p>`;
  else if (dr.snaps) snaps = dr.snaps.value.length ? `<div class="list">${dr.snaps.value.map(s => `<div><span>${esc(s.name)} <span class="muted small">${s.snaptime ? new Date(s.snaptime * 1000).toLocaleString() : ""} ${esc(s.description || "")}</span></span>
      <span class="actions"><button data-rollback="${esc(s.name)}" ${c.job ? "disabled" : ""}>Roll back</button><button class="danger" data-delsnap="${esc(s.name)}" ${c.job ? "disabled" : ""}>Delete</button></span></div>`).join("")}</div>` : `<p class="muted">No snapshots.</p>`;
  let jobs = `<p class="muted">Loading…</p>`;
  if (dr.jobs?.status === "fulfilled") jobs = dr.jobs.value.length ? `<div class="list">${dr.jobs.value.map(j => `<div class="jobrow"><span>${esc(KIND_LABEL[j.kind] || j.kind)}${j.auto ? ' <span class="badge" title="Started by automatic updates">auto</span>' : ""}<span class="muted small jobwhen">${new Date(j.created_at * 1000).toLocaleString()}</span></span>
      <span class="actions"><span class="badge ${j.status === "success" ? "ok" : j.status === "failed" ? "bad" : "warn"}">${esc(j.status)}${j.rc != null && j.rc !== 0 ? ` (${j.rc})` : ""}</span><button data-job="${esc(j.id)}">Log</button></span></div>`).join("")}</div>` : `<p class="muted">No jobs yet.</p>`;
  $("#dbody").innerHTML = `
    <div class="dactions">
      <button data-act="app" data-id="${c.vmid}" ${c.status !== "running" || c.job || !c.script ? "disabled" : ""} class="${c.outdated ? "primary" : ""}">Update app</button>
      <button data-act="os" data-id="${c.vmid}" ${c.status !== "running" || c.job || !c.pkgmgr ? "disabled" : ""} class="${c.pkg_count ? "primary" : ""}">Update packages</button>
      <button data-restart="${c.vmid}" ${c.status !== "running" || c.job ? "disabled" : ""} class="${c.restart_needed ? "primary" : ""}">Restart</button>
      <button data-rescan="${c.vmid}" ${c.scanning || c.status !== "running" ? "disabled" : ""}>${c.scanning ? "Scanning…" : "Rescan"}</button>
      ${c.job ? `<button data-job="${esc(c.job)}">Live log</button>` : ""}
    </div>
    ${uptimeHtml(c)}
    ${c.error ? `<p class="err">${esc(c.error)}</p>` : ""}
    <h3>App versions</h3>${scriptLinks}${comps}
    ${retired(c) ? `<div class="whatsnew small" style="border-color:color-mix(in srgb, var(--bad) 35%, var(--border));background:color-mix(in srgb, var(--bad) 6%, var(--panel))">
      <b>The ${esc(c.script)} script was retired by community-scripts.</b> ${esc(c.script_state.message || "")}
      <div class="muted" style="margin-top:4px">If it was replaced by another script, its update may ask a question before switching over (Jellyseerr and Overseerr ask to migrate to Seerr). lxcwatch can't answer prompts, so run <code>update</code> once yourself in the container's console, then rescan.</div></div>` : ""}
    ${c.components.some(x => x.missing_version) ? `<p class="whatsnew small">${c.components.filter(x => x.missing_version).map(x => `<b>${esc(x.name)}</b> has no version file (<code>${esc(x.version_file)}</code>) yet, so lxcwatch can't tell which version is installed. This is normal right after a script migration. The next app update installs the latest release and records it.`).join(" ")}</p>` : ""}
    ${(c.failures || []).map(f => `<div class="whatsnew small" style="${f.superseded ? "" : "border-color:color-mix(in srgb, var(--bad) 35%, var(--border));background:color-mix(in srgb, var(--bad) 6%, var(--panel))"}">
      <div class="head"><b>A community-script failed in this container${f.code != null ? ` (exit ${f.code})` : ""}${f.at ? `, ${new Date(f.at * 1000).toLocaleString()}` : ""}</b>
        <button data-clearfail="${c.vmid}" ${c.job ? "disabled" : ""}>Dismiss</button></div>
      <div class="muted">${f.superseded ? '<span class="badge ok">resolved</span> A later run succeeded, so this is no longer flagged as a problem. ' : ""}Left behind as <code>/root/${esc(f.file)}</code>. A successful app update clears it too.</div>
      ${f.errinfo.length ? `<pre class="log" style="margin-top:8px;max-height:240px">${esc(f.errinfo.join("\n"))}</pre>` : ""}
    </div>`).join("")}
    ${c.restart_needed ? `<p class="whatsnew small"><b>Restart needed.</b> ${c.stale_procs.length ? `These are still running code that an update replaced: <code>${esc(c.stale_procs.join(", "))}</code>` : "The container reports that it needs a reboot."}</p>` : ""}
    ${c.release ? `<h3>Release upgrade</h3><div class="whatsnew small"><div class="head">
      <span><b>Debian ${c.release.current} (${esc(c.release.from)}) → ${c.release.target} (${esc(c.release.to)})</b> is available${c.release.target < c.release.stable ? `, one step towards Debian ${c.release.stable}` : ""}.</span>
      <button class="primary" data-release="${c.vmid}" ${c.status !== "running" || c.job ? "disabled" : ""}>Upgrade to Debian ${c.release.target}…</button></div>
      <div class="muted" style="margin-top:4px">Runs pre-checks first. Switches the official Debian sources, upgrades every package and restarts the container.</div></div>` : ""}
    <h3>Pending packages (${c.pkg_count})</h3>${c.pkg_stale ? staleHtml(c) : ""}${pk}
    <h3>Automatic updates</h3>${autoHtml(c)}
    <div class="sectionhead"><h3>Snapshots</h3><button data-snapshot="${c.vmid}" ${c.job ? "disabled" : ""}>Take snapshot…</button></div>${snaps}
    <h3>Job history</h3>${jobs}`;
}

function staleHtml(c) {
  const repos = c.pkg_refresh_errors || [];
  return `<div class="whatsnew small" style="border-color:color-mix(in srgb, var(--bad) 35%, var(--border));background:color-mix(in srgb, var(--bad) 6%, var(--panel))">
    <b>${repos.length ? `${repos.length === 1 ? "A package source" : `${repos.length} package sources`} couldn't be refreshed` : "The package lists couldn't be refreshed"}</b>,
    so the container may have updates that aren't listed here.
    ${repos.length ? `<div class="list" style="margin-top:6px">${repos.map(r => `<div><code>${esc(r.repo)}</code><span class="badge bad">${esc(r.reason)}</span></div>`).join("")}</div>` : ""}
    ${repos.some(r => r.reason === "signature check failed") ? `<div class="muted" style="margin-top:6px">A failed signature check usually means the repo's signing key changed or uses SHA1, which current Debian rejects. Download the vendor's current key over the keyring file the source's <code>signed-by</code> points to.</div>` : ""}
    ${c.pkg_warnings?.length ? `<details style="margin-top:6px"><summary class="muted">apt output</summary><pre class="log" style="max-height:200px">${esc(c.pkg_warnings.join("\n"))}</pre></details>` : ""}
  </div>`;
}

const notesKey = x => `${x.forge}|${x.repo}|${x.installed}|${x.target}`;

function compDetail(x, dr) {
  const ver = `installed ${esc(x.installed ?? "?")} · target ${x.release_url ? `<a href="${esc(x.release_url)}" target="_blank" rel="noopener">${esc(x.target ?? "?")}</a>` : esc(x.target ?? "?")}${
    x.upstream && x.upstream !== x.target ? ` · upstream ${x.upstream_url ? `<a href="${esc(x.upstream_url)}" target="_blank" rel="noopener">${esc(x.upstream)}</a>` : esc(x.upstream)}` : ""}`;
  const ext = (href, text) => `<a class="ext" href="${esc(href)}" target="_blank" rel="noopener">${text}</a>`;
  const linkList = [
    x.source === "apt" ? `<span class="muted small">apt package${x.origin ? ` from ${esc(x.origin)}` : ""}</span>` : "",
    x.release_url ? ext(x.release_url, `Release ${esc(x.target_tag)}`) : "",
    x.repo ? ext(x.releases_url, "All releases") + ext(x.repo_url, esc(x.repo)) : "",
    x.homepage && !(x.repo_url && x.homepage.startsWith(x.repo_url)) ? ext(x.homepage, "Project site") : "",
    x.package_url ? ext(x.package_url, `${esc(x.origin)} package`) : "",
  ].join("");
  const links = linkList ? `<div class="links">${linkList}</div>` : "";
  let news = "";
  if (x.state === "outdated" && x.repo) {
    const key = notesKey(x);
    const n = dr.notes[key];
    if (!n) news = `<button class="small" data-notes="${esc(key)}">What's new since ${esc(x.notes_installed || x.installed)}?</button>`;
    else if (n === "loading") news = `<div class="whatsnew muted small">Loading releases…</div>`;
    else if (n.error) news = `<div class="whatsnew err small">${esc(n.error)}</div>`;
    else {
      const rels = n.releases.map(r => {
        const id = `${key}|${r.tag}`;
        return `<details class="rel" data-open="${esc(id)}" ${dr.open.has(id) ? "open" : ""}>
          <summary><span><b>${esc(r.name)}</b>${r.name !== r.tag ? ` <span class="muted small">${esc(r.tag)}</span>` : ""}</span>
            <span class="small"><span class="muted">${r.published ? new Date(r.published).toLocaleDateString() : ""}</span> <a class="ext" href="${esc(r.url)}" target="_blank" rel="noopener">open</a></span></summary>
          <pre>${esc(r.notes || "No release notes.")}${r.notes_truncated ? "\n…" : ""}</pre></details>`;
      }).join("");
      news = `<div class="whatsnew">
        <div class="head small"><b>${n.releases.length} release${n.releases.length === 1 ? "" : "s"} to catch up on${n.complete ? "" : " (older ones not shown)"}</b>
          ${n.compare_url ? `<a class="ext" href="${esc(n.compare_url)}" target="_blank" rel="noopener">Compare ${esc(n.installed_tag)}…${esc(n.target_tag)}</a>` : ""}</div>
        ${rels || `<p class="muted small">No matching releases found.</p>`}</div>`;
    }
  }
  return `<div class="comp-d"><div class="head"><span><b>${esc(x.name)}</b> ${x.state === "outdated" ? '<span class="badge warn">update</span>' : x.state === "current" ? '<span class="badge ok">current</span>' : `<span class="badge info">${esc(x.state)}</span>`}</span>
    <span class="small">${ver}</span></div>${links}${news}</div>`;
}

async function loadNotes(key) {
  const dr = state.drawer;
  const c = state.data.containers.find(x => x.vmid === dr.vmid);
  const x = c?.components.find(y => notesKey(y) === key);
  if (!x) return;
  dr.notes[key] = "loading";
  renderDetails(false);
  try {
    const q = new URLSearchParams({ forge: x.forge, repo: x.repo, installed: x.notes_installed || x.installed, target: x.target_tag || x.notes_target || x.target });
    dr.notes[key] = await api(`/api/releases?${q}`);
  } catch (e) {
    dr.notes[key] = { error: e.message };
  }
  if (state.drawer === dr) renderDetails(false);
}

function openJob(id, vmid, from = "ct") {
  if (state.es) state.es.close();
  state.drawer = { type: "job", id, vmid };
  const c = state.data?.containers.find(x => x.vmid === vmid);
  openDrawer(`Job log${c ? ` · ${c.vmid} ${c.name}` : ""}`, id);
  $("#dbody").innerHTML = `<div class="actions" style="margin-bottom:10px"><span id="jstatus" class="badge warn">connecting…</span>${from === "activity" ? `<button data-backactivity="1">Back to activity</button>` : vmid ? `<button data-back="${vmid}">Back to container</button>` : ""}</div><pre class="log" id="jlog"></pre>`;
  const pre = $("#jlog");
  const es = new EventSource(`/api/jobs/${encodeURIComponent(id)}/stream`);
  state.es = es;
  es.onopen = () => { $("#jstatus").textContent = "running"; };
  es.onmessage = ev => {
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += JSON.parse(ev.data) + "\n";
    if (stick) pre.scrollTop = pre.scrollHeight;
  };
  es.addEventListener("done", ev => {
    const r = JSON.parse(ev.data);
    const st = $("#jstatus");
    st.textContent = r.status + (r.rc != null && r.rc !== 0 ? ` (exit ${r.rc})` : "");
    st.className = `badge ${r.status === "success" ? "ok" : "bad"}`;
    es.close();
    if (state.es === es) state.es = null;
    refresh();
  });
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) $("#jstatus").textContent = "disconnected"; };
}
