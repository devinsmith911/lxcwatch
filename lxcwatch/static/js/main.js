document.addEventListener("click", async ev => {
  const t = ev.target.closest("button, input[type=checkbox], input[type=radio], .steps [data-job], a[data-ctlink]");
  if (!t) return;
  if (t.matches("[data-f]")) {
    state.filter = state.filter === t.dataset.f ? "all" : t.dataset.f;
    state.page = 1;
    render();
    // On mobile the boxes scroll sideways: bring the selected one into view
    // (scrollLeft only, so the page itself doesn't jump).
    const strip = $("#stats"), active = strip.querySelector(".stat.active");
    if (active && (active.offsetLeft < strip.scrollLeft || active.offsetLeft + active.offsetWidth > strip.scrollLeft + strip.clientWidth)) {
      strip.scrollTo({ left: active.offsetLeft - 12, behavior: "smooth" });
    }
  } else if (t.matches("[data-sel]")) {
    const id = Number(t.dataset.sel);
    t.checked ? state.selected.add(id) : state.selected.delete(id);
    render();
  } else if (t.id === "selall") {
    const size = Number(state.pageSize) || Infinity;
    const onPage = visible().slice((state.page - 1) * size, state.page * size);
    for (const c of onPage) t.checked ? state.selected.add(c.vmid) : state.selected.delete(c.vmid);
    render();
  } else if (t.dataset.act === "details") {
    openDetails(Number(t.dataset.id));
  } else if (t.dataset.act) {
    startUpdate(t.dataset.act, [Number(t.dataset.id)]);
  } else if (t.id === "bulkapp" || t.id === "bulkos") {
    const kind = t.id === "bulkapp" ? "app" : "os";
    const ids = state.data.containers.filter(c => state.selected.has(c.vmid) && c.status === "running" && !c.job && (kind === "os" ? c.pkgmgr : c.script)).map(c => c.vmid);
    if (!ids.length) return alert("None of the selected containers can run that update.");
    startUpdate(kind, ids);
  } else if (t.id === "bulkauto") {
    openAutoBulk();
  } else if (t.dataset.autopol) {
    const c = state.data.containers.find(x => x.vmid === Number(t.dataset.id));
    setAutoPolicy([c.vmid], t.dataset.autopol, c.auto?.rollback || false).catch(e => alert(e.message));
  } else if (t.dataset.autorb) {
    const c = state.data.containers.find(x => x.vmid === Number(t.dataset.autorb));
    setAutoPolicy([c.vmid], c.auto.policy, t.checked).catch(e => alert(e.message));
  } else if (t.dataset.autoresume) {
    api(`/api/containers/${t.dataset.autoresume}/auto/resume`, { method: "POST" }).then(refresh).catch(e => alert(e.message));
  } else if (t.id === "bulkclear") {
    state.selected.clear(); render();
  } else if (t.id === "scanbtn") {
    await api("/api/scan", { method: "POST" }).catch(e => alert(e.message));
    refresh();
  } else if (t.id === "dclose") {
    closeDrawer();
  } else if (t.dataset.job) {
    openJob(t.dataset.job, t.dataset.jobvmid ? Number(t.dataset.jobvmid) : state.drawer?.vmid);
  } else if (t.dataset.notes) {
    loadNotes(t.dataset.notes);
  } else if (t.id === "activitybtn") {
    openActivity();
  } else if (t.dataset.jobfilter) {
    state.drawer.filter = state.activityFilter = t.dataset.jobfilter; renderActivity();
  } else if (t.dataset.joblog) {
    openJob(t.dataset.joblog, Number(t.dataset.jobvmid), "activity");
  } else if (t.dataset.ctlink) {
    openDetails(Number(t.dataset.ctlink));
  } else if (t.dataset.backactivity) {
    openActivity();
  } else if (t.dataset.back) {
    openDetails(Number(t.dataset.back));
  } else if (t.dataset.rescan) {
    const id = Number(t.dataset.rescan);
    const c = state.data.containers.find(x => x.vmid === id); if (c) c.scanning = true; render();
    await api(`/api/containers/${id}/scan`, { method: "POST" }).catch(e => alert(e.message));
    refresh();
  } else if (t.dataset.release) {
    openReleaseUpgrade(Number(t.dataset.release));
  } else if (t.id === "updateall") {
    openUpdateAll();
  } else if (t.id === "batchstop") {
    state.batchStopping = true; render();
    await api("/api/batch/stop", { method: "POST" }).catch(e => alert(e.message));
  } else if (t.id === "batchdismiss") {
    await api("/api/batch/dismiss", { method: "POST" }).catch(() => {});
    refresh();
  } else if (t.dataset.allpick) {
    const id = Number(t.dataset.allpick);
    t.checked ? allState.off.delete(id) : allState.off.add(id);
    renderAllDialog();
  } else if (t.id === "allapp" || t.id === "allos") {
    renderAllDialog();
  } else if (t.dataset.clearfail) {
    const vmid = Number(t.dataset.clearfail);
    if (!confirm(`Dismiss the failure flag in CT ${vmid}? The install log stays.`)) return;
    t.disabled = true;
    try { await api(`/api/containers/${vmid}/clear-failures`, { method: "POST" }); await refresh(); }
    catch (e) { alert(e.message); t.disabled = false; }
  } else if (t.dataset.restart) {
    const vmid = Number(t.dataset.restart);
    if (!confirm(`Restart CT ${vmid}? Its services will be briefly unavailable.`)) return;
    try { const r = await api(`/api/containers/${vmid}/restart`, { method: "POST" }); openJob(r.job, vmid); refresh(); }
    catch (e) { alert(e.message); }
  } else if (t.dataset.snapshot) {
    openSnapshot(Number(t.dataset.snapshot));
  } else if (t.dataset.rollback) {
    const vmid = state.drawer.vmid, name = t.dataset.rollback;
    if (!confirm(`Roll CT ${vmid} back to ${name}? Everything changed since that snapshot is lost. The container is restarted if it was running.`)) return;
    try { const r = await api(`/api/containers/${vmid}/snapshots/${encodeURIComponent(name)}/rollback`, { method: "POST" }); openJob(r.job, vmid); refresh(); }
    catch (e) { alert(e.message); }
  } else if (t.dataset.delsnap) {
    const vmid = state.drawer.vmid, name = t.dataset.delsnap;
    if (!confirm(`Delete snapshot ${name} of CT ${vmid}?`)) return;
    t.disabled = true;
    try { await api(`/api/containers/${vmid}/snapshots/${encodeURIComponent(name)}`, { method: "DELETE" }); openDetails(vmid); }
    catch (e) { alert(e.message); t.disabled = false; }
  }
});
$("#scrim").addEventListener("click", closeDrawer);
$("#q").addEventListener("input", e => { state.q = e.target.value; state.page = 1; render(); });
$("#pagesize").value = String(state.pageSize);
$("#pagesize").addEventListener("change", e => { state.pageSize = Number(e.target.value); store("pageSize", state.pageSize); state.page = 1; render(); });
$("#prev").addEventListener("click", () => { state.page--; render(); scrollTo({ top: 0, behavior: "smooth" }); });
$("#next").addEventListener("click", () => { state.page++; render(); scrollTo({ top: 0, behavior: "smooth" }); });
const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  document.querySelectorAll("[data-themeset]").forEach(b => b.classList.toggle("on", b.dataset.themeset === t));
}
let theme = stored("theme", "system");
if (!THEMES.includes(theme)) theme = "system";
applyTheme(theme);
$("#settingsbtn").addEventListener("click", openSettings);
document.addEventListener("click", e => {
  const a = e.target.closest("a[data-opensettings]");
  if (a) { e.preventDefault(); openSettings(); }
});
$("#logoutbtn").addEventListener("click", async () => {
  let target = "/login";
  try { target = (await api("/api/logout", { method: "POST" })).redirect; } catch {}
  location.replace(target);
});
document.querySelectorAll("[data-themeset]").forEach(b => b.addEventListener("click", () => {
  theme = b.dataset.themeset;
  store("theme", theme);
  applyTheme(theme);
}));
for (const [id, key] of [["hidestopped", "hideStopped"], ["hidenonscript", "hideNonScript"]]) {
  $(`#${id}`).checked = state[key];
  $(`#${id}`).addEventListener("change", e => { state[key] = e.target.checked; store(key, state[key]); state.page = 1; render(); });
}
document.addEventListener("click", e => {
  for (const id of ["conn", "menu"]) if (!e.target.closest(`#${id}`)) $(`#${id}`).open = false;
});
// Remember which release notes are expanded across the periodic re-render.
document.addEventListener("toggle", e => {
  const id = e.target.dataset?.open;
  if (!id || state.drawer?.type !== "ct") return;
  e.target.open ? state.drawer.open.add(id) : state.drawer.open.delete(id);
}, true);
document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#drawer").classList.contains("open") && !$("#confirm").open) closeDrawer(); });

refresh();
setInterval(refresh, 5000);
