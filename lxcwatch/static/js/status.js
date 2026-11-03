function renderConn(conn) {
  if (!conn) return;
  const el = $("#conn");
  const online = conn.nodes.filter(n => n.online).length;
  const where = `${esc(conn.user)}@${esc(conn.host)}${conn.port !== 22 ? `:${conn.port}` : ""}`;
  const label = conn.ok === null ? `Connecting to ${where}…`
    : conn.ok ? `${esc(conn.cluster || conn.host)} · ${online}/${conn.nodes.length} node${conn.nodes.length === 1 ? "" : "s"}`
    : `Can't reach ${where}`;
  const dot = conn.ok === null ? "" : conn.ok && online === conn.nodes.length ? "ok" : "bad";
  const short = conn.ok === null ? "…" : conn.ok ? `${online}/${conn.nodes.length} nodes` : "offline";
  const summary = `<span class="dot ${dot}"></span><span class="conn-long">${label}</span><span class="conn-short">${short}</span>`;
  const pop = `<div class="pop">
      <div><b>Connected via</b><span>${where}</span></div>
      ${conn.cluster ? `<div><span class="muted">Cluster</span><span>${esc(conn.cluster)}</span></div>` : ""}
      <div><span class="muted">Last contact</span><span>${conn.at ? ago(conn.at) : "never"}</span></div>
      ${conn.error ? `<div class="err small">${esc(conn.error)}</div>` : ""}
      ${conn.nodes.length ? `<hr style="border:0;border-top:1px solid var(--border)">` : ""}
      ${conn.nodes.map(n => `<div><span><span class="dot ${n.online ? "ok" : "bad"}"></span> ${esc(n.name)}${n.local ? ' <span class="muted small">(entry)</span>' : ""}</span><span class="muted">${esc(n.address)}</span></div>`).join("")}
    </div>`;
  // Update in place so an open popover stays open across refreshes.
  const html = summary + pop;
  if (el.dataset.html !== html) {
    el.dataset.html = html;
    el.querySelector("summary").innerHTML = summary;
    el.querySelector(".pop")?.remove();
    el.insertAdjacentHTML("beforeend", pop);
  }
}

function renderAccess(email) {
  $("#accessbadge").hidden = $("#accessrow").hidden = !email;
  $("#accessbadge").title = email ? `Signed in as ${email} through Cloudflare Access` : "";
  $("#accessemail").textContent = email || "";
}

async function refresh() {
  try {
    state.data = await api("/api/containers");
    const ids = new Set(state.data.containers.map(c => c.vmid));
    for (const id of state.selected) if (!ids.has(id)) state.selected.delete(id);
    render();
  } catch (e) {
    $("#scaninfo").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
}

function confirmUpdate(kind, vmids) {
  const cs = state.data.containers.filter(c => vmids.includes(c.vmid));
  $("#ctitle").textContent = kind === "app" ? `Run the app update on ${cs.length} container${cs.length > 1 ? "s" : ""}?` : `Update packages on ${cs.length} container${cs.length > 1 ? "s" : ""}?`;
  $("#cbody").innerHTML = `<div class="list">${cs.map(c => {
    const what = kind === "app"
      ? (c.components.filter(x => x.state === "outdated").map(x => `${esc(x.name)} ${esc(x.installed)} → ${esc(x.target)}${x.release_url ? ` <a class="ext" href="${esc(x.release_url)}" target="_blank" rel="noopener">notes</a>` : ""}`).join(", ") || "runs <code>update</code> (also updates packages)")
      : `${c.pkg_count} package${c.pkg_count === 1 ? "" : "s"}`;
    return `<div><span>${c.vmid} · ${esc(c.name)}</span><span class="muted small">${what}</span></div>`;
  }).join("")}</div>`;
  $("#csnap").checked = $("#bulksnap").checked;
  $("#crestartwrap").hidden = kind !== "os";
  $("#crestart").value = stored("restart", "auto");
  const dlg = $("#confirm");
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok" ? { snapshot: $("#csnap").checked, restart: $("#crestart").value } : null), { once: true }));
}

async function startUpdate(kind, vmids) {
  const ok = await confirmUpdate(kind, vmids);
  if (!ok) return;
  try {
    if (kind === "os") store("restart", ok.restart);
    const r = await api("/api/update", { method: "POST", body: JSON.stringify({ vmids, kind, snapshot: ok.snapshot, restart: ok.restart }) });
    const errs = Object.entries(r.errors);
    if (errs.length) alert(errs.map(([id, e]) => `${id}: ${e}`).join("\n"));
    const started = Object.entries(r.jobs);
    if (started.length === 1) openJob(started[0][1], Number(started[0][0]));
    state.selected.clear();
    await refresh();
  } catch (e) { alert(e.message); }
}
