// ---- Update all ----
const allState = { off: new Set() };
const appPending = c => c.status === "running" && c.script && c.outdated > 0;
const osPending = c => c.status === "running" && c.pkgmgr && c.pkg_count > 0;

function renderAllDialog() {
  const cs = state.data.containers;
  const app = $("#allapp").checked, os = $("#allos").checked;
  $("#allappn").textContent = `(${cs.filter(appPending).length})`;
  $("#allosn").textContent = `(${cs.filter(osPending).length})`;
  $("#allrestartwrap").hidden = !os;
  const rows = cs.filter(c => (app && appPending(c)) || (os && osPending(c)));
  $("#allpick").innerHTML = rows.length ? rows.map(c => {
    const what = [
      app && appPending(c) ? c.components.filter(x => x.state === "outdated").map(x => `${esc(x.name)} ${esc(x.installed)} → ${esc(x.target)}`).join(", ") : "",
      os && osPending(c) ? `${c.pkg_count} package${c.pkg_count === 1 ? "" : "s"}${c.pkg_security ? ` (${c.pkg_security} security)` : ""}` : "",
    ].filter(Boolean).join(" · ");
    return `<label><span><input type="checkbox" data-allpick="${c.vmid}" ${allState.off.has(c.vmid) ? "" : "checked"}> ${c.vmid} · ${esc(c.name)}</span><span class="muted small">${what}</span></label>`;
  }).join("") : `<div class="muted small" style="padding:10px">Nothing to update. Everything selected is up to date.</div>`;
  const picked = rows.filter(c => !allState.off.has(c.vmid));
  const steps = picked.reduce((n, c) => n + (app && appPending(c) ? 1 : 0) + (os && osPending(c) ? 1 : 0), 0);
  $("#allsummary").textContent = picked.length ? `${steps} update${steps === 1 ? "" : "s"} across ${picked.length} container${picked.length === 1 ? "" : "s"}` : "";
  $("#allgo").disabled = !picked.length;
  $("#allgo").textContent = picked.length ? `Update ${picked.length} container${picked.length === 1 ? "" : "s"}` : "Start";
  return picked;
}

async function openUpdateAll() {
  if (state.data.batch?.status === "running") return alert("An update-all run is already in progress.");
  allState.off.clear();
  $("#allsnap").checked = false;
  $("#allrestart").value = stored("restart", "auto");
  renderAllDialog();
  const dlg = $("#alldlg");
  dlg.returnValue = "";
  dlg.showModal();
  const ok = await new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
  if (!ok) return;
  const app = $("#allapp").checked, os = $("#allos").checked;
  // Only send each container the kinds it actually needs.
  const picked = renderAllDialog();
  const kinds = [app && "app", os && "os"].filter(Boolean);
  try {
    if (os) store("restart", $("#allrestart").value);
    const appIds = picked.filter(c => app && appPending(c)).map(c => c.vmid);
    const osIds = picked.filter(c => os && osPending(c)).map(c => c.vmid);
    const both = appIds.filter(id => osIds.includes(id));
    const order = picked.map(c => c.vmid);
    await api("/api/batch", { method: "POST", body: JSON.stringify({
      vmids: order, kinds, snapshot: $("#allsnap").checked, restart: $("#allrestart").value, stop_on_failure: $("#allstop").checked,
      only: Object.fromEntries(order.map(id => [id, both.includes(id) ? ["app", "os"] : appIds.includes(id) ? ["app"] : ["os"]])),
    }) });
    refresh();
  } catch (e) { alert(e.message); }
}
