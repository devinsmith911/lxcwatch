const KIND_LABEL = { app: "app update", os: "package update", release: "release upgrade", restart: "restart", rollback: "rollback", snapshot: "snapshot" };
const STEP_BADGE = { pending: "", running: "warn", success: "ok", failed: "bad", skipped: "info" };
const STEP_ICON = { pending: "○", running: "◐", success: "✓", failed: "✗", skipped: "–" };

function renderBatch(b) {
  const box = $("#batchbox");
  if (!b) { box.innerHTML = ""; return; }
  const done = b.steps.filter(s => !["pending", "running"].includes(s.status)).length;
  const failed = b.steps.filter(s => s.status === "failed").length;
  const cur = b.steps.find(s => s.status === "running");
  const title = b.status === "running"
    ? `${b.auto ? "Automatic updates" : "Updating all"} · ${done} of ${b.steps.length} done${cur ? ` · now ${cur.vmid} ${esc(cur.name)} (${cur.kind === "app" ? "app" : "packages"})` : ""}`
    : `${b.auto ? "Automatic updates" : "Update all"} ${b.status === "done" ? "finished" : b.status === "stopped" ? "stopped" : "failed"} · ${b.steps.filter(s => s.status === "success").length} succeeded${failed ? `, ${failed} failed` : ""}${b.steps.some(s => s.status === "skipped") ? `, ${b.steps.filter(s => s.status === "skipped").length} skipped` : ""}`;
  box.innerHTML = `<div class="batch">
    <div class="head"><b>${title}</b>
      ${b.status === "running" ? `<button id="batchstop" ${state.batchStopping ? "disabled" : ""}>${state.batchStopping ? "Stopping after current…" : "Stop"}</button>` : `<button id="batchdismiss">Dismiss</button>`}</div>
    <div class="bar"><div style="width:${Math.round(done / b.steps.length * 100)}%"></div></div>
    <div class="steps">${b.steps.map(s => `<span class="badge ${STEP_BADGE[s.status]}" ${s.job ? `data-job="${esc(s.job)}" data-jobvmid="${s.vmid}" title="Show log"` : ""} ${s.note ? `title="${esc(s.note)}"` : ""}>${STEP_ICON[s.status]} ${s.vmid} ${esc(s.name)} · ${s.kind === "app" ? "app" : "packages"}${s.note && s.status !== "running" ? ` — ${esc(s.note)}` : ""}</span>`).join("")}</div>
  </div>`;
  if (b.status !== "running") state.batchStopping = false;
}

const CHECK_ICON = { ok: "✓", warn: "⚠", block: "⛔" };

async function openReleaseUpgrade(vmid) {
  const c = state.data.containers.find(x => x.vmid === vmid);
  if (!c?.release) return;
  const dlg = $("#reldlg");
  $("#reltitle").textContent = `Upgrade ${c.vmid} · ${c.name} to Debian ${c.release.target} (${c.release.to})?`;
  $("#relbody").innerHTML = `<p class="muted">Running pre-checks…</p>`;
  $("#relsnap").checked = true;
  $("#relgo").disabled = true;
  dlg.returnValue = "";
  dlg.showModal();
  const closed = new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
  try {
    const r = await api(`/api/containers/${vmid}/release-check`);
    $("#relbody").innerHTML = `<div class="list">${r.checks.map(ch => `<div><span><span class="badge ${ch.level === "ok" ? "ok" : ch.level === "warn" ? "warn" : "bad"}">${CHECK_ICON[ch.level]}</span> ${esc(ch.text)}</span></div>`).join("")}</div>
      ${r.blocked ? `<p class="err">Fix the ⛔ items before upgrading.</p>` : ""}`;
    $("#relgo").disabled = r.blocked;
    $("#relgo").textContent = `Upgrade to Debian ${c.release.target}`;
  } catch (e) {
    $("#relbody").innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
  if (!(await closed)) return;
  try {
    const r = await api(`/api/containers/${vmid}/release-upgrade`, { method: "POST", body: JSON.stringify({ snapshot: $("#relsnap").checked }) });
    openJob(r.job, vmid);
    refresh();
  } catch (e) { alert(e.message); }
}

const SNAP_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/;

async function openSnapshot(vmid) {
  const c = state.data.containers.find(x => x.vmid === vmid);
  if (!c) return;
  const d = new Date(), p = n => String(n).padStart(2, "0");
  $("#snaptitle").textContent = `Take a snapshot of ${c.vmid} · ${c.name}`;
  $("#snapname").value = `manual-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  $("#snapdesc").value = "";
  $("#snaperr").textContent = "";
  const dlg = $("#snapdlg");
  dlg.returnValue = "";
  dlg.showModal();
  $("#snapname").select();
  while (true) {
    const ok = await new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
    if (!ok) return;
    const name = $("#snapname").value.trim();
    const error = !SNAP_NAME_RE.test(name) ? "Use a letter first, then letters, digits, - or _ (up to 40)."
      : name.startsWith("lxcwatch-") ? "Names starting with lxcwatch- are for automatic snapshots, which get cleaned up." : "";
    if (!error) {
      try {
        const r = await api(`/api/containers/${vmid}/snapshots`, { method: "POST", body: JSON.stringify({ name, description: $("#snapdesc").value }) });
        openJob(r.job, vmid);
        refresh();
        return;
      } catch (e) { $("#snaperr").textContent = e.message; }
    } else {
      $("#snaperr").textContent = error;
    }
    dlg.returnValue = "";
    dlg.showModal();
  }
}
