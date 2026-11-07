// Modal dialogs. openDialog() in actions.js opens one and resolves with its result.
import { AUTO_POLICIES, Ext, appPending, api, fmtWhen, html, osPending, plural, post, state, store, stored, useEffect, useLayoutEffect, useRef, useState } from "./core.js";
import { attempt, dialogClosed, openDialog, openJob, refresh } from "./actions.js";

// A native <dialog>. The form's button value says what happened: "cancel" closes
// it, anything else calls onOk(close), which closes it with a result when done.
function Modal({ id, onOk, onOpen, children }) {
  const dlg = useRef(), result = useRef(null);
  useLayoutEffect(() => { dlg.current.showModal(); onOpen?.(); }, []);
  const close = value => { result.current = value; dlg.current.close(); };
  const submit = e => {
    e.preventDefault();
    if (e.submitter?.value === "cancel") close(null);
    else onOk(close);
  };
  return html`<dialog id=${id} ref=${dlg} onClose=${() => dialogClosed(result.current)}><form method="dialog" onSubmit=${submit}>${children}</form></dialog>`;
}

const RestartOptions = ({ value }) => [["auto", "Only if needed"], ["always", "Always"], ["never", "Never"]].map(([v, label]) =>
  html`<option key=${v} value=${v} selected=${v === value}>${label}</option>`);
const END = "flex-direction:row-reverse;justify-content:flex-start";

// ---- Confirm an update ----
function Confirm({ kind, cs }) {
  const [snapshot, setSnapshot] = useState(state.bulkSnapshot);
  const [restart, setRestart] = useState(stored("restart", "auto"));
  const n = cs.length;
  return html`<${Modal} id="confirm" onOk=${close => close({ snapshot, restart })}>
    <h3 id="ctitle" style="margin-top:0">${kind === "app" ? `Run the app update on ${n} container${n > 1 ? "s" : ""}?` : `Update packages on ${n} container${n > 1 ? "s" : ""}?`}</h3>
    <div id="cbody"><div class="list">${cs.map(c => html`<div key=${c.vmid}><span>${c.vmid} · ${c.name}</span><span class="muted small">${kind === "app" ? html`<${AppChanges} c=${c} />` : plural(c.pkg_count, "package")}</span></div>`)}</div></div>
    <p><label class="check" id="csnapwrap"><input type="checkbox" id="csnap" checked=${snapshot} onChange=${e => setSnapshot(e.target.checked)} /> Take a snapshot first</label></p>
    <p id="crestartwrap" hidden=${kind !== "os"}><label class="check">Restart afterwards${" "}
      <select id="crestart" onChange=${e => setRestart(e.target.value)}><${RestartOptions} value=${restart} /></select></label>${" "}
      <span class="muted small" style="display:block;margin-top:4px">"Only if needed" restarts the container when services are still running replaced libraries or binaries.</span></p>
    <div class="actions" style="justify-content:flex-end">
      <button value="cancel">Cancel</button>
      <button value="ok" class="primary" id="cok">Update</button>
    </div>
  <//>`;
}

function AppChanges({ c }) {
  const out = c.components.filter(x => x.state === "outdated");
  if (!out.length) return html`runs <code>update</code> (also updates packages)`;
  return out.map((x, i) => html`${i > 0 && ", "}${x.name} ${x.installed} → ${x.target}${x.release_url && html` <${Ext} href=${x.release_url}>notes<//>`}`);
}

// ---- Update all ----
// Which kinds are ticked and "stop at first failure" carry over between openings.
const allChoice = { app: true, os: true, stop: false };

function allPicked(off) {
  const rows = state.data.containers.filter(c => (allChoice.app && appPending(c)) || (allChoice.os && osPending(c)));
  return { rows, picked: rows.filter(c => !off.has(c.vmid)) };
}

function UpdateAll() {
  const [off, setOff] = useState(new Set());
  const [snapshot, setSnapshot] = useState(false);
  const [restart, setRestart] = useState(stored("restart", "auto"));
  const [, redraw] = useState(0);
  const { app, os, stop } = allChoice;
  const cs = state.data.containers;
  const { rows, picked } = allPicked(off);
  const steps = picked.reduce((n, c) => n + (app && appPending(c) ? 1 : 0) + (os && osPending(c) ? 1 : 0), 0);
  const choose = key => e => { allChoice[key] = e.target.checked; redraw(n => n + 1); };
  const togglePick = (id, on) => { const next = new Set(off); on ? next.delete(id) : next.add(id); setOff(next); };
  const what = c => [
    app && appPending(c) ? c.components.filter(x => x.state === "outdated").map(x => `${x.name} ${x.installed} → ${x.target}`).join(", ") : "",
    os && osPending(c) ? `${plural(c.pkg_count, "package")}${c.pkg_security ? ` (${c.pkg_security} security)` : ""}` : "",
  ].filter(Boolean).join(" · ");
  const start = close => close(allPicked(off).picked.length ? { picked: allPicked(off).picked, snapshot, restart } : null);
  return html`<${Modal} id="alldlg" onOk=${start}>
    <h3 style="margin-top:0">Update all</h3>
    <div class="actions">
      <label class="toggle"><input type="checkbox" id="allapp" checked=${app} onChange=${choose("app")} /> Apps <span class="muted small" id="allappn">(${cs.filter(appPending).length})</span></label>
      <label class="toggle"><input type="checkbox" id="allos" checked=${os} onChange=${choose("os")} /> Packages <span class="muted small" id="allosn">(${cs.filter(osPending).length})</span></label>
    </div>
    <div class="picklist" id="allpick">${rows.length ? rows.map(c => html`<label key=${c.vmid}><span><input type="checkbox" checked=${!off.has(c.vmid)} onChange=${e => togglePick(c.vmid, e.target.checked)} /> ${c.vmid} · ${c.name}</span><span class="muted small">${what(c)}</span></label>`)
      : html`<div class="muted small" style="padding:10px">Nothing to update. Everything selected is up to date.</div>`}</div>
    <div class="muted small" id="allsummary">${picked.length ? `${plural(steps, "update")} across ${plural(picked.length, "container")}` : ""}</div>
    <p><label class="check"><input type="checkbox" id="allsnap" checked=${snapshot} onChange=${e => setSnapshot(e.target.checked)} /> Take a snapshot before each update</label></p>
    <p id="allrestartwrap" hidden=${!os}><label class="check">Restart after package updates${" "}
      <select id="allrestart" onChange=${e => setRestart(e.target.value)}><${RestartOptions} value=${restart} /></select></label></p>
    <p><label class="check"><input type="checkbox" id="allstop" checked=${stop} onChange=${choose("stop")} /> Stop at the first failure</label></p>
    <p class="muted small">Runs one container at a time; with both selected, each container's app update runs before its package update. It keeps going if you close this page.</p>
    <div class="actions" style="justify-content:flex-end">
      <button value="cancel">Cancel</button>
      <button value="ok" class="primary" id="allgo" disabled=${!picked.length}>${picked.length ? `Update ${plural(picked.length, "container")}` : "Start"}</button>
    </div>
  <//>`;
}

export async function openUpdateAll() {
  if (!state.data) return;
  if (state.data.batch?.status === "running") return alert("An update-all run is already in progress.");
  const ok = await openDialog("all");
  if (!ok) return;
  const { app, os, stop } = allChoice;
  // Only send each container the kinds it actually needs.
  const kinds = [app && "app", os && "os"].filter(Boolean);
  try {
    if (os) store("restart", ok.restart);
    const appIds = ok.picked.filter(c => app && appPending(c)).map(c => c.vmid);
    const osIds = ok.picked.filter(c => os && osPending(c)).map(c => c.vmid);
    const both = appIds.filter(id => osIds.includes(id));
    const order = ok.picked.map(c => c.vmid);
    await post("/api/batch", {
      vmids: order, kinds, snapshot: ok.snapshot, restart: ok.restart, stop_on_failure: stop,
      only: Object.fromEntries(order.map(id => [id, both.includes(id) ? ["app", "os"] : appIds.includes(id) ? ["app"] : ["os"]])),
    });
    refresh();
  } catch (e) { alert(e.message); }
}

// ---- Automatic updates (the global settings) ----
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function runSummary(run) {
  if (!run) return "No automatic run yet.";
  const n = st => run.steps.filter(s => s.status === st).length;
  const when = fmtWhen(run.started_at);
  if (run.status === "running") return `Running now (started ${when}).`;
  const skipped = run.skipped.length + n("skipped");
  return `Last run ${when}: ${n("success")} updated${n("failed") ? `, ${n("failed")} failed` : ""}${skipped ? `, ${skipped} skipped` : ""}${run.error ? `. Error: ${run.error}` : ""}.`;
}

// Notification fields. Secrets never come back from the server: the field is
// empty with a "saved" hint, and is only sent when you type or remove it.
const NOTIFY_KEYS = ["ntfy_url", "ntfy_token", "pushover_token", "pushover_user", "slack_url", "webhook_url", "packages", "public_url"];

function notifyPayload(cur, n) {
  const out = {};
  for (const k of NOTIFY_KEYS) if (!cur.locked[k] && n[k] !== undefined) out[k] = n[k];
  return out;
}

function NotifyField({ k, label, placeholder, cur, n, setN }) {
  const locked = cur.locked[k], secret = k in cur.secrets_set;
  if (locked) {
    return html`<span class="nfield"><input class="field" disabled aria-label=${label} value=${secret ? "" : cur.values[k]}
      placeholder=${secret ? "Set" : ""} /><span class="muted small">Set by ${locked}</span></span>`;
  }
  const saved = secret && cur.secrets_set[k] && n[k] === undefined;
  return html`<span class="nfield"><input class="field" type=${secret ? "password" : "url"} autocomplete="off" spellcheck="false" aria-label=${label}
      value=${n[k] ?? ""} placeholder=${saved ? "Saved · type to replace" : placeholder} onInput=${e => setN({ ...n, [k]: e.target.value })} />
    ${saved && html`<button type="button" class="small" title="Forget the saved ${label.toLowerCase()}" onClick=${() => setN({ ...n, [k]: "" })}>Remove</button>`}</span>`;
}

function NotifyTest({ cur, n }) {
  const [res, setRes] = useState(null);
  const run = async () => {
    setRes("sending");
    try { setRes((await post("/api/notify/test", { notify: notifyPayload(cur, n) })).results); }
    catch (e) { setRes(e.message); }
  };
  return html`<div class="ntest">
    <button type="button" id="notifytest" disabled=${res === "sending"} onClick=${run}>${res === "sending" ? "Sending…" : "Send a test"}</button>
    ${Array.isArray(res) ? res.map(r => html`<span key=${r.channel} class="badge ${r.ok ? "ok" : "bad"}" title=${r.error || undefined}>${r.ok ? "✓" : "✗"} ${r.channel}${r.error ? `: ${r.error}` : ""}</span>`)
      : res && res !== "sending" && html`<span class="err small">${res}</span>`}
  </div>`;
}

function Notifications({ cur }) {
  // Plain values start filled in; secrets start untouched (undefined).
  const [n, setN] = useState(Object.fromEntries(NOTIFY_KEYS.filter(k => !(k in cur.secrets_set)).map(k => [k, cur.values[k]])));
  const [err, setErr] = useState("");
  const nf = (k, label, placeholder) => html`<${NotifyField} k=${k} label=${label} placeholder=${placeholder} cur=${cur} n=${n} setN=${setN} />`;
  const save = async close => {
    try {
      await api("/api/notify", { method: "PUT", body: JSON.stringify({ notify: notifyPayload(cur, n) }) });
      close(true);
    } catch (e) { setErr(e.message); }
  };
  return html`<${Modal} id="notifydlg" onOk=${save}>
    <h3 style="margin-top:0">Notifications</h3>
    <p class="muted small">New app updates, package updates and new lxcwatch versions are sent once each to every channel you set up, along with the results of automatic updates.</p>
    <div class="setgrid" id="notifygrid">
      <span>ntfy</span>
      <span class="stack">${nf("ntfy_url", "Topic URL", "https://ntfy.sh/your-topic")}${nf("ntfy_token", "Access token", "Access token (optional)")}</span>
      <span>Pushover</span>
      <span class="stack">${nf("pushover_token", "Application token", "Application token")}${nf("pushover_user", "User key", "User key")}</span>
      <span>Slack</span>
      <span class="stack">${nf("slack_url", "Webhook URL", "https://hooks.slack.com/services/…")}
        <span class="muted small">An incoming webhook: in your Slack app, turn on <b>Incoming Webhooks</b> and add one for a channel.</span></span>
      <span>Webhook</span>
      <span class="stack">${nf("webhook_url", "Webhook URL", "https://…")}
        <span class="muted small">Gets a JSON POST: <code>{title, message, items, url}</code></span></span>
      <label for="notifypkgs">Packages</label>
      <span class="stack">${cur.locked.packages
        ? html`<span class="nfield"><input class="field" disabled value=${cur.values.packages} /><span class="muted small">Set by ${cur.locked.packages}</span></span>`
        : html`<select id="notifypkgs" onChange=${e => setN({ ...n, packages: e.target.value })}>${[["security", "Security updates only"], ["all", "All package updates"], ["none", "Don't notify about packages"]]
          .map(([v, label]) => html`<option key=${v} value=${v} selected=${n.packages === v}>${label}</option>`)}</select>`}
        <span class="muted small">App updates are always included.</span></span>
      <span>Link</span>
      <span class="stack">${nf("public_url", "lxcwatch address", "https://lxcwatch.example.com")}
        <span class="muted small">lxcwatch's address, so notifications can link back here.</span></span>
    </div>
    <${NotifyTest} cur=${cur} n=${n} />
    <p class="err small" id="notifyerr">${err}</p>
    <div class="actions" style=${END}>
      <button value="ok" class="primary">Save</button>
      <button value="cancel" formnovalidate>Cancel</button>
    </div>
  <//>`;
}

export async function openNotifications() {
  document.querySelector("#menu").open = false;
  try { await openDialog("notifications", { cur: await api("/api/notify") }); }
  catch (e) { alert(e.message); }
}

function Settings({ current }) {
  const a = current.auto;
  const [f, setF] = useState({ enabled: a.enabled, start: a.start, end: a.end, days: a.days, age: a.min_age_days, snap: a.snapshot_packages });
  const [err, setErr] = useState("");
  const set = (key, prop = "value") => e => setF({ ...f, [key]: e.target[prop] });
  const setDay = (i, on) => setF({ ...f, days: on ? [...f.days, i] : f.days.filter(d => d !== i) });
  const save = async close => {
    const auto = { enabled: f.enabled, start: f.start, end: f.end, days: DAY_NAMES.map((_, i) => i).filter(i => f.days.includes(i)), min_age_days: Number(f.age), snapshot_packages: f.snap };
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ auto }) });
      close(true);
      refresh();
    } catch (e) { setErr(e.message); }
  };
  return html`<${Modal} id="setdlg" onOk=${save}>
    <h3 style="margin-top:0">Automatic updates</h3>
    <p><label class="check"><input type="checkbox" id="setauto" checked=${f.enabled} onChange=${set("enabled", "checked")} /> Install updates automatically in a maintenance window</label></p>
    <p class="muted small" style="margin-top:-4px">Only containers you opt in are updated. Choose what each one gets in its Details, or select several in the table and use <b>Automatic updates</b> in the bar that appears.</p>
    <div class="setgrid">
      <label for="setstart">Window</label>
      <span><input type="time" id="setstart" class="field short" required value=${f.start} onInput=${set("start")} /> to <input type="time" id="setend" class="field short" required value=${f.end} onInput=${set("end")} />
        <div class="muted small" id="settz">Server time, ${current.timezone || ""} (UTC${current.utc_offset.slice(0, 3)}:${current.utc_offset.slice(3)}), now ${new Date(current.server_time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} here</div></span>
      <span>Days</span>
      <span class="days" id="setdays">${DAY_NAMES.map((d, i) => html`<label key=${d}><input type="checkbox" value=${i} checked=${f.days.includes(i)} onChange=${e => setDay(i, e.target.checked)} />${d}</label>`)}</span>
      <label for="setage">New app releases</label>
      <span>Wait <input type="number" id="setage" class="field tiny" min="0" max="60" step="1" required value=${f.age} onInput=${set("age")} /> days before installing
        <div class="muted small">Broken releases usually get a fix within a few days. Package updates don't wait.</div></span>
      <span>Snapshots</span>
      <span><label class="check"><input type="checkbox" id="setsnap" checked=${f.snap} onChange=${set("snap", "checked")} /> Also before package-only updates</label>
        <div class="muted small">App updates always take one first.</div></span>
    </div>
    <div class="whatsnew small" id="setstatus">${current.status.next_at && html`Next window: <b>${fmtWhen(current.status.next_at)}</b>. `}${runSummary(current.status.last_run)}</div>
    <p class="err small" id="seterr">${err}</p>
    <div class="actions" style=${END}>
      <button value="ok" class="primary">Save</button>
      <button value="cancel" formnovalidate>Cancel</button>
    </div>
  <//>`;
}

export async function openSettings() {
  document.querySelector("#menu").open = false;
  try { await openDialog("settings", { current: await api("/api/settings") }); }
  catch (e) { alert(e.message); }
}

// ---- Automatic updates for several containers ----
function AutoBulk({ count, cs }) {
  const policies = new Set(cs.map(c => c.auto?.policy || "off"));
  const [policy, setPolicy] = useState(policies.size === 1 ? [...policies][0] : "");
  const [rollback, setRollback] = useState(cs.length > 0 && cs.every(c => c.auto?.rollback));
  const auto = state.data.auto;
  return html`<${Modal} id="autodlg" onOk=${close => close(policy ? { policy, rollback } : null)}>
    <h3 style="margin-top:0" id="autotitle">Automatic updates for ${plural(count, "container")}</h3>
    <div class="autopick" id="autopick">${AUTO_POLICIES.map(([k, , long, desc]) => html`<label key=${k}><input type="radio" name="autopol" value=${k} checked=${policy === k} onChange=${() => setPolicy(k)} /><b>${long}</b><span class="muted small">${desc}</span></label>`)}</div>
    <p><label class="check"><input type="checkbox" id="autorb" checked=${rollback} onChange=${e => setRollback(e.target.checked)} /> Roll back to the snapshot if an update fails</label></p>
    <p class="muted small" id="autonote">${auto?.enabled ? `Runs in the maintenance window; the next one starts ${auto.next_at ? fmtWhen(auto.next_at) : "soon"}.` : "Automatic updates are turned off (Automatic updates in the menu). This only sets what each container would get."}</p>
    <div class="actions" style=${END}>
      <button value="ok" class="primary">Apply</button>
      <button value="cancel">Cancel</button>
    </div>
  <//>`;
}

// ---- Take a snapshot ----
const SNAP_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/;

function snapshotName() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  return `manual-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

function Snapshot({ c }) {
  const [name, setName] = useState(snapshotName);
  const [description, setDescription] = useState("");
  const [err, setErr] = useState("");
  const nameInput = useRef();
  const take = async close => {
    const clean = name.trim();
    const problem = !SNAP_NAME_RE.test(clean) ? "Use a letter first, then letters, digits, - or _ (up to 40)."
      : clean.startsWith("lxcwatch-") ? "Names starting with lxcwatch- are for automatic snapshots, which get cleaned up." : "";
    if (problem) { setErr(problem); nameInput.current.focus(); return; }
    try {
      const r = await post(`/api/containers/${c.vmid}/snapshots`, { name: clean, description });
      close(true);
      openJob(r.job, c.vmid);
      refresh();
    } catch (e) { setErr(e.message); nameInput.current.focus(); }
  };
  return html`<${Modal} id="snapdlg" onOk=${take} onOpen=${() => nameInput.current.select()}>
    <h3 style="margin-top:0" id="snaptitle">Take a snapshot of ${c.vmid} · ${c.name}</h3>
    <p><label class="small" for="snapname"><b>Name</b></label><br />
      <input id="snapname" class="field" maxlength="40" autocomplete="off" spellcheck="false" ref=${nameInput} value=${name} onInput=${e => setName(e.target.value)} /></p>
    <p><label class="small" for="snapdesc"><b>Description</b> <span class="muted">(optional)</span></label><br />
      <input id="snapdesc" class="field" maxlength="200" placeholder="e.g. before changing the config" value=${description} onInput=${e => setDescription(e.target.value)} /></p>
    <p class="muted small">Starts with a letter; letters, digits, - and _ only. Manual snapshots are never removed automatically. Delete them here when you no longer need them.</p>
    <p class="err small" id="snaperr">${err}</p>
    ${/* Primary button first so Enter takes the snapshot; row-reverse keeps it on the right. */ ""}
    <div class="actions" style=${END}>
      <button value="ok" class="primary" id="snapgo">Take snapshot</button>
      <button value="cancel">Cancel</button>
    </div>
  <//>`;
}

export function openSnapshot(c) {
  openDialog("snapshot", { c });
}

// ---- Debian release upgrade ----
const CHECK_ICON = { ok: "✓", warn: "⚠", block: "⛔" };
const CHECK_BADGE = { ok: "ok", warn: "warn" };

function ReleaseUpgrade({ c }) {
  const [check, setCheck] = useState(null);
  const [snapshot, setSnapshot] = useState(true);
  useEffect(() => {
    api(`/api/containers/${c.vmid}/release-check`).then(setCheck, e => setCheck({ error: e.message }));
  }, []);
  const ready = check && !check.error;
  const body = !check ? html`<p class="muted">Running pre-checks…</p>`
    : check.error ? html`<p class="err">${check.error}</p>`
    : html`<div class="list">${check.checks.map(ch => html`<div key=${ch.text}><span><span class="badge ${CHECK_BADGE[ch.level] || "bad"}">${CHECK_ICON[ch.level]}</span> ${ch.text}</span></div>`)}</div>
      ${check.blocked && html`<p class="err">Fix the ⛔ items before upgrading.</p>`}`;
  return html`<${Modal} id="reldlg" onOk=${close => close({ snapshot })}>
    <h3 style="margin-top:0" id="reltitle">Upgrade ${c.vmid} · ${c.name} to Debian ${c.release.target} (${c.release.to})?</h3>
    <div id="relbody" class="small">${body}</div>
    <p><label class="check"><input type="checkbox" id="relsnap" checked=${snapshot} onChange=${e => setSnapshot(e.target.checked)} /> Take a snapshot first (strongly recommended)</label></p>
    <p class="muted small">Takes several minutes. The container restarts at the end. Apps that depend on third-party repos or specific library versions can break. Check them afterwards, and roll back to the snapshot if needed.</p>
    <div class="actions" style="justify-content:flex-end">
      <button value="cancel">Cancel</button>
      <button value="ok" class="primary" id="relgo" disabled=${!ready || check.blocked}>${ready ? `Upgrade to Debian ${c.release.target}` : "Upgrade"}</button>
    </div>
  <//>`;
}

export async function openReleaseUpgrade(c) {
  if (!c.release) return;
  const ok = await openDialog("release", { c });
  if (!ok) return;
  try {
    const r = await post(`/api/containers/${c.vmid}/release-upgrade`, { snapshot: ok.snapshot });
    openJob(r.job, c.vmid);
    refresh();
  } catch (e) { alert(e.message); }
}

const DIALOGS = { confirm: Confirm, all: UpdateAll, settings: Settings, notifications: Notifications, auto: AutoBulk, snapshot: Snapshot, release: ReleaseUpgrade };

export function Dialogs() {
  const d = state.dialog;
  return d && html`<${DIALOGS[d.type]} key=${d.key} ...${d.props} />`;
}
