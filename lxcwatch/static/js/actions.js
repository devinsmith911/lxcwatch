// What the buttons do: loading data, opening the drawer and dialogs, starting jobs.
import { api, container, post, state, store, update } from "./core.js";

export async function refresh() {
  try {
    state.data = await api("/api/containers");
    state.error = null;
    const ids = new Set(state.data.containers.map(c => c.vmid));
    for (const id of state.selected) if (!ids.has(id)) state.selected.delete(id);
    if (state.drawer?.type === "ct" && !ids.has(state.drawer.vmid)) closeDrawer();
    if (state.drawer?.type === "activity") loadActivity();
  } catch (e) {
    state.error = e.message;
  }
  update();
}

// Runs an API call for a button, reporting a failure rather than throwing.
export const attempt = fn => fn().catch(e => alert(e.message));

// ---- Dialogs: main.js renders state.dialog; the promise resolves when it closes ----
let dialogKey = 0;
export const openDialog = (type, props = {}) => new Promise(resolve => {
  state.dialog = { type, props, key: ++dialogKey, resolve };
  update();
});
export function dialogClosed(value) {
  const d = state.dialog;
  state.dialog = null;
  update();
  d?.resolve(value);
}

// ---- The side drawer ----
let drawerKey = 0;
function openDrawer(drawer) {
  state.drawer?.stop?.();
  state.drawer = { ...drawer, key: ++drawerKey };
  update();
  return state.drawer;
}
export function closeDrawer() {
  state.drawer?.stop?.();
  state.drawer = null;
  update();
}

export async function openDetails(vmid) {
  const c = container(vmid);
  if (!c) return closeDrawer();
  const dr = openDrawer({ type: "ct", vmid, snaps: null, jobs: null, notes: {},
    title: `${c.vmid} · ${c.name}`, sub: `${c.node} · ${c.os || ""}${c.script ? ` · ct/${c.script}.sh` : ""}` });
  const [snaps, jobs] = await Promise.allSettled([api(`/api/containers/${vmid}/snapshots`), api(`/api/jobs?vmid=${vmid}`)]);
  dr.snaps = snaps;
  dr.jobs = jobs;
  update();
}

export function openActivity() {
  openDrawer({ type: "activity", filter: state.activityFilter, jobs: null, title: "Activity", sub: "Recent jobs across all containers" });
  loadActivity();
}

async function loadActivity() {
  const dr = state.drawer;
  try { dr.jobs = await api("/api/jobs?limit=200"); dr.error = null; }
  catch (e) { dr.error = e.message; }
  update();
}

export function openJob(id, vmid, from = "ct") {
  const c = state.data && container(vmid);
  openDrawer({ type: "job", id, vmid, from, title: `Job log${c ? ` · ${c.vmid} ${c.name}` : ""}`, sub: id });
}

// ---- Updates ----
export async function startUpdate(kind, vmids) {
  const cs = state.data.containers.filter(c => vmids.includes(c.vmid));
  const ok = await openDialog("confirm", { kind, cs });
  if (!ok) return;
  try {
    if (kind === "os") store("restart", ok.restart);
    const r = await post("/api/update", { vmids, kind, snapshot: ok.snapshot, restart: ok.restart });
    const errs = Object.entries(r.errors);
    if (errs.length) alert(errs.map(([id, e]) => `${id}: ${e}`).join("\n"));
    const started = Object.entries(r.jobs);
    if (started.length === 1) openJob(started[0][1], Number(started[0][0]));
    state.selected.clear();
    await refresh();
  } catch (e) { alert(e.message); }
}

// Starts a job from the details panel and shows its log.
export async function runJob(path, vmid) {
  try {
    const r = await post(path);
    openJob(r.job, vmid);
    refresh();
  } catch (e) { alert(e.message); }
}

export async function setAutoPolicy(vmids, policy, rollback) {
  await post("/api/auto/policy", { vmids, policy, rollback });
  await refresh();
}
