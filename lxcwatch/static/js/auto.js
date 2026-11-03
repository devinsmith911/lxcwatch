// ---- Automatic updates ----
const AUTO_POLICIES = [
  ["off", "Off", "Off", "Nothing is installed automatically."],
  ["security", "Security", "Security packages", "Only package updates from security repos."],
  ["packages", "Packages", "Packages", "All pending package updates."],
  ["app", "App + packages", "App and packages", "The app update, then all package updates."],
];
const AUTO_NAME = Object.fromEntries(AUTO_POLICIES.map(([k, , long]) => [k, long]));
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const fmtWhen = t => new Date(t * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function autoBadge(c) {
  if (!c.auto || c.auto.policy === "off") return "";
  if (c.auto.paused) return ` <span class="badge warn" title="Automatic updates paused: ${esc(c.auto.paused_reason || "")}">auto paused</span>`;
  const on = state.data.auto?.enabled;
  return ` <span class="badge${on ? "" : " muted"}" title="Automatic updates: ${esc(AUTO_NAME[c.auto.policy])}${on ? "" : " (turned off in Settings)"}">auto</span>`;
}

function autoHtml(c) {
  const a = c.auto || { policy: "off" }, g = state.data.auto || {};
  const seg = `<div class="seg wide" role="group" aria-label="Automatic updates">${AUTO_POLICIES.map(([k, short, , desc]) =>
    `<button class="${a.policy === k ? "on" : ""}" data-autopol="${k}" data-id="${c.vmid}" title="${esc(desc)}">${short}</button>`).join("")}</div>`;
  const parts = [seg];
  if (a.policy !== "off") {
    parts.push(`<label class="check small"><input type="checkbox" data-autorb="${c.vmid}" ${a.rollback ? "checked" : ""}> Roll back to the snapshot if an update fails</label>`);
    if (a.paused) parts.push(`<div class="whatsnew small" style="border-color:color-mix(in srgb, var(--bad) 35%, var(--border));background:color-mix(in srgb, var(--bad) 6%, var(--panel))">
      <div class="head"><span><b>Paused</b>: ${esc(a.paused_reason || "an automatic update failed")}. A successful manual update resumes it too.</span>
        <button data-autoresume="${c.vmid}">Resume</button></div></div>`);
    else if (!g.enabled) parts.push(`<p class="muted small">Automatic updates are turned off in <a href="#" data-opensettings="1">Settings</a>, so nothing runs yet.</p>`);
    else if (g.next_at) parts.push(`<p class="muted small">Next window: ${fmtWhen(g.next_at)}${c.lxcwatch_host ? ". lxcwatch runs in this container: its own package and restarts are left to you." : ""}</p>`);
  } else {
    parts.push(`<p class="muted small">${esc(AUTO_POLICIES[0][3])}</p>`);
  }
  if (a.last) parts.push(`<p class="small">Last automatic run: <span class="badge ${STEP_BADGE[a.last.status] || ""}">${esc(a.last.status)}</span>${a.last.note ? ` <span class="muted">${esc(a.last.note)}</span>` : ""}</p>`);
  return `<div class="autobox">${parts.join("")}</div>`;
}

async function setAutoPolicy(vmids, policy, rollback) {
  await api("/api/auto/policy", { method: "POST", body: JSON.stringify({ vmids, policy, rollback }) });
  await refresh();
}

function runSummary(run) {
  if (!run) return "No automatic run yet.";
  const n = st => run.steps.filter(s => s.status === st).length;
  const when = fmtWhen(run.started_at);
  if (run.status === "running") return `Running now (started ${when}).`;
  return `Last run ${when}: ${n("success")} updated${n("failed") ? `, ${n("failed")} failed` : ""}${run.skipped.length + n("skipped") ? `, ${run.skipped.length + n("skipped")} skipped` : ""}${run.error ? `. Error: ${esc(run.error)}` : ""}.`;
}

async function openSettings() {
  $("#menu").open = false;
  const dlg = $("#setdlg");
  let r;
  try { r = await api("/api/settings"); } catch (e) { return alert(e.message); }
  const a = r.auto;
  $("#setauto").checked = a.enabled;
  $("#setstart").value = a.start;
  $("#setend").value = a.end;
  $("#setage").value = a.min_age_days;
  $("#setsnap").checked = a.snapshot_packages;
  $("#setdays").innerHTML = DAY_NAMES.map((d, i) => `<label><input type="checkbox" value="${i}" ${a.days.includes(i) ? "checked" : ""}>${d}</label>`).join("");
  $("#settz").textContent = `Server time, ${r.timezone || ""} (UTC${r.utc_offset.slice(0, 3)}:${r.utc_offset.slice(3)}), now ${new Date(r.server_time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} here`;
  $("#setstatus").innerHTML = `${r.status.next_at ? `Next window: <b>${fmtWhen(r.status.next_at)}</b>. ` : ""}${runSummary(r.status.last_run)}`;
  $("#seterr").textContent = "";
  dlg.returnValue = "";
  dlg.showModal();
  while (true) {
    const ok = await new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
    if (!ok) return;
    const body = { auto: {
      enabled: $("#setauto").checked, start: $("#setstart").value, end: $("#setend").value,
      days: [...$("#setdays").querySelectorAll("input:checked")].map(x => Number(x.value)),
      min_age_days: Number($("#setage").value), snapshot_packages: $("#setsnap").checked,
    } };
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
      refresh();
      return;
    } catch (e) { $("#seterr").textContent = e.message; }
    dlg.returnValue = "";
    dlg.showModal();
  }
}

async function openAutoBulk() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const cs = state.data.containers.filter(c => state.selected.has(c.vmid));
  const current = new Set(cs.map(c => c.auto?.policy || "off"));
  const pick = current.size === 1 ? [...current][0] : "";
  $("#autotitle").textContent = `Automatic updates for ${ids.length} container${ids.length === 1 ? "" : "s"}`;
  $("#autopick").innerHTML = AUTO_POLICIES.map(([k, , long, desc]) =>
    `<label><input type="radio" name="autopol" value="${k}" ${pick === k ? "checked" : ""}><b>${long}</b><span class="muted small">${esc(desc)}</span></label>`).join("");
  $("#autorb").checked = cs.length > 0 && cs.every(c => c.auto?.rollback);
  $("#autonote").textContent = state.data.auto?.enabled ? `Runs in the maintenance window; the next one starts ${state.data.auto.next_at ? fmtWhen(state.data.auto.next_at) : "soon"}.`
    : "Automatic updates are turned off in Settings. This only sets what each container would get.";
  const dlg = $("#autodlg");
  dlg.returnValue = "";
  dlg.showModal();
  const ok = await new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
  const chosen = $("#autopick").querySelector("input:checked");
  if (!ok || !chosen) return;
  try { await setAutoPolicy(ids, chosen.value, $("#autorb").checked); } catch (e) { alert(e.message); }
}
