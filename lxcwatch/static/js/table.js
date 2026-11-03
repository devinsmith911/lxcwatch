const needsUpdate = c => c.outdated > 0 || c.pkg_count > 0;
// Running, but we can't say what app version it has.
// Scanned fine, but there's no community-scripts `update` in it.
const notScript = c => !!c.os && !c.script;
// A community-script (or not yet scanned) container whose app version we can't tell.
const isUnknown = c => c.status === "running" && !notScript(c) && (!c.scanned_at || !c.os
  || (c.components.length > 0 && c.components.every(x => x.state === "unknown" || x.state === "untracked")));
const rowState = c => c.status !== "running" ? "stopped" : c.pkg_security ? "security" : needsUpdate(c) ? "outdated" : isUnknown(c) || notScript(c) || c.pkg_stale ? "unknown" : "current";
// community-scripts reports removed scripts as state "deleted" or found: false with a message.
const retired = c => !!c.script_state && (c.script_state.state === "deleted" || (c.script_state.found === false && !!c.script_state.message));
const problem = c => c.error || c.failures?.some(f => !f.superseded) || c.upstream_errors.length || c.components.some(x => x.pending) || retired(c) || c.last_job?.status === "failed" || c.pkg_stale;

function visible() {
  const q = state.q.toLowerCase();
  return state.data.containers.filter(c => {
    if (state.hideStopped && c.status !== "running") return false;
    if (state.hideNonScript && notScript(c)) return false;
    if (state.filter === "unknown" && !isUnknown(c)) return false;
    if (state.filter === "app" && !c.outdated) return false;
    if (state.filter === "packages" && !c.pkg_count) return false;
    if (state.filter === "security" && !c.pkg_security) return false;
    if (state.filter === "problems" && !problem(c)) return false;
    if (q && ![c.vmid, c.name, c.node, ...c.tags, c.script].join(" ").toLowerCase().includes(q)) return false;
    return true;
  });
}

// Apps installed with apt: a small tag naming where the package came from.
const aptTag = x => x.source === "apt" ? ` <span class="badge" title="Installed with apt${x.origin ? ` from ${esc(x.origin)}` : ""}">apt</span>` : "";

function compHtml(c) {
  if (c.status !== "running" && !c.components.length) return `<span class="muted dash">—</span>`;
  if (!c.script && !c.components.length) return `<span class="badge info">not a community-script</span>`;
  const parts = c.components.map(x => {
    const name = esc(x.name) + aptTag(x);
    if (x.state === "outdated") {
      const target = x.release_url ? `<a href="${esc(x.release_url)}" target="_blank" rel="noopener" title="Release notes for ${esc(x.target_tag)}">${esc(x.target)}</a>` : esc(x.target);
      return `<div class="comp" title="${esc(x.repo)}">${name} ${esc(x.installed)} <span class="arrow">→ ${target}</span>${x.pinned ? ' <span class="badge">pinned</span>' : ""}${x.release_url ? ` <a class="small ext" href="${esc(x.release_url)}" target="_blank" rel="noopener">notes</a>` : ""}</div>`;
    }
    if (x.state === "current") {
      const behind = x.pinned && x.upstream && x.upstream !== x.target ? ` <span class="badge" title="community-scripts pins ${esc(x.target)}; upstream has ${esc(x.upstream)}">pinned</span>` : "";
      return `<div class="comp">${name} ${esc(x.installed)} <span class="badge ok">current</span>${behind}</div>`;
    }
    if (x.missing_version) return `<div class="comp" title="No ${esc(x.version_file)} yet; the next app update records it">${name} <span class="badge info">version unknown</span>${x.target ? ` <span class="muted small">latest ${esc(x.target)}</span>` : ""}</div>`;
    return `<div class="comp">${name} ${esc(x.installed ?? "?")} <span class="badge info">${x.state === "untracked" ? "untracked" : "unknown"}</span></div>`;
  });
  if (c.components.some(x => x.pending)) parts.push(`<span class="badge bad">last update did not finish</span>`);
  if (retired(c)) parts.push(`<span class="badge bad" title="${esc(c.script_state.message || "")}">script retired</span>`);
  if (!parts.length) parts.push(`<span class="muted small">${esc(c.script)} · updates via packages</span>`);
  return parts.join("");
}

function pkgHtml(c) {
  if (!c.pkgmgr) return `<span class="muted dash">—</span>`;
  const restart = (c.restart_needed ? ` <span class="badge warn" title="${esc(c.stale_procs.length ? `Still running replaced code: ${c.stale_procs.join(", ")}` : "The container asks for a reboot")}">restart needed</span>` : "")
    + (c.release ? ` <span class="badge info" title="Release upgrade available, see Details">Debian ${c.release.target} available</span>` : "");
  const stale = c.pkg_stale ? ` <span class="badge bad" title="Package lists couldn't be refreshed${c.pkg_refresh_errors?.length ? `: ${esc(c.pkg_refresh_errors.map(e => e.repo).join(", "))}` : ""}. See Details.">lists stale</span>` : "";
  if (!c.pkg_count) return `${c.pkg_stale ? "" : `<span class="badge ok">up to date</span>`}${stale}${restart}`;
  return `<span class="badge warn">${c.pkg_count} pending</span>${c.pkg_security ? ` <span class="badge bad">${c.pkg_security} security</span>` : ""}${stale}${restart}`;
}

function rowHtml(c) {
  const running = c.status === "running";
  const busy = !!c.job;
  const status = running ? "" : ` <span class="badge">${esc(c.status)}</span>`;
  const err = c.error ? `<div class="err small">${esc(c.error)}</div>` : "";
  const upErr = c.upstream_errors.length ? `<div class="err small">${esc(c.upstream_errors.join("; "))}</div>` : "";
  // Update buttons only appear when there's something to update; Details always
  // has them for a manual run.
  const job = busy ? `<span class="badge warn">updating…</span>` : c.last_job?.status === "failed" ? `<span class="badge bad">last ${esc(KIND_LABEL[c.last_job.kind] || c.last_job.kind)} failed</span>` : "";
  return `<tr class="r-${rowState(c)}${state.selected.has(c.vmid) ? " selected" : ""}">
    <td><input type="checkbox" data-sel="${c.vmid}" ${state.selected.has(c.vmid) ? "checked" : ""} aria-label="Select ${c.vmid}"></td>
    <td><div class="name"><span class="dot" title="${rowState(c)}"></span>${c.vmid} · ${esc(c.name)}${status}${autoBadge(c)} <span class="badge node only-mobile">${esc(c.node)}</span></div>
        <div class="small ctinfo"><span class="badge node">${esc(c.node)}</span> <span class="muted">${esc(c.os || "")}</span></div>
        ${c.tags.length ? `<div class="tags">${c.tags.map(t => `<span class="badge">${esc(t)}</span>`).join("")}</div>` : ""}${err}</td>
    <td data-label="App:">${compHtml(c)}${upErr}</td>
    <td data-label="OS:">${pkgHtml(c)}</td>
    <td data-label="Scanned:" class="small muted">${c.scanning ? "scanning…" : ago(c.scanned_at)}</td>
    <td><div class="actions">
      ${job}
      ${running && !busy && c.script && (c.outdated || c.components.some(x => x.missing_version)) ? `<button data-act="app" data-id="${c.vmid}" class="primary">Update app</button>` : ""}
      ${running && !busy && c.pkgmgr && c.pkg_count ? `<button data-act="os" data-id="${c.vmid}">Update packages</button>` : ""}
      <button data-act="details" data-id="${c.vmid}">Details</button>
    </div></td></tr>`;
}

function render() {
  const d = state.data;
  if (!d) return;
  const cs = d.containers;
  const s = d.scan;
  renderConn(d.connection);
  renderAccess(d.access_email);
  $("#version").textContent = d.version && d.version !== "dev" ? d.version : "development build";
  renderBatch(d.batch);
  const next = s.next_at ? ` · next ${until(s.next_at)}` : s.interval_minutes > 0 ? "" : " · scheduled scans off";
  $("#scaninfo").textContent = s.running ? "Scanning…" : `${s.last ? `Last scan ${ago(s.last.at)}` : "Not scanned yet"}${next}`;
  if (s.error) $("#scaninfo").innerHTML += ` · <span class="badge bad">${esc(s.error)}</span>`;
  $("#scanbtn").disabled = s.running;
  const running = cs.filter(c => c.status === "running").length;
  // Each box filters the table; clicking the active one shows everything again.
  const stats = [
    ["all", "Containers", cs.length, "--accent", `${running} running · ${cs.length - running} stopped`],
    ["app", "App updates", cs.filter(c => c.outdated).length, "--warn"],
    ["packages", "Package updates", cs.filter(c => c.pkg_count).length, "--info"],
    ["security", "Security", cs.filter(c => c.pkg_security).length, "--bad"],
    ["problems", "Problems", cs.filter(problem).length, "--bad"],
    ["unknown", "Unknown version", cs.filter(isUnknown).length, "--idle"],
  ];
  $("#stats").innerHTML = stats.map(([f, k, v, color, sub]) => {
    const live = v || f === "all";
    return `<button class="stat${live ? "" : " zero"}${state.filter === f ? " active" : ""}" data-f="${f}" aria-pressed="${state.filter === f}"
      style="--c: var(${live ? color : "--idle"})"><b>${v}</b><span class="label">${k}</span>${sub ? `<span class="sub">${sub}</span>` : ""}</button>`;
  }).join("");
  const rows = visible();
  const size = Number(state.pageSize) || rows.length || 1;
  const pages = Math.max(1, Math.ceil(rows.length / size));
  state.page = Math.min(Math.max(1, state.page), pages);
  const start = (state.page - 1) * size;
  const shown = rows.slice(start, start + size);
  $("#rows").innerHTML = shown.length ? shown.map(rowHtml).join("") : `<tr class="empty"><td colspan="6" class="muted">Nothing here.</td></tr>`;
  $("#selall").checked = shown.length > 0 && shown.every(c => state.selected.has(c.vmid));
  $("#pageinfo").textContent = rows.length ? `Showing ${start + 1}–${start + shown.length} of ${rows.length}${rows.length < cs.length ? ` (${cs.length - rows.length} filtered out)` : ""}` : `0 of ${cs.length} containers match`;
  $("#pagenum").textContent = `Page ${state.page} of ${pages}`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = state.page >= pages;
  const n = state.selected.size;
  $("#bulk").classList.toggle("show", n > 0);
  $("#bulkcount").textContent = `${n} selected`;
  if (state.drawer?.type === "ct") renderDetails(false);
  if (state.drawer?.type === "activity") loadActivity();
  const su = d.self_update, el = $("#selfupdate");
  el.hidden = !su;
  if (su) {
    el.innerHTML = `<span class="lbl-long">${esc(su.version)} available</span><span class="lbl-short">↑ ${esc(su.version)}</span>`;
    el.title = `lxcwatch ${su.version} is out (you have ${su.current}). Upgrade with apt update && apt upgrade.`;
    if (su.url) el.href = su.url; else el.removeAttribute("href");
  }
}
