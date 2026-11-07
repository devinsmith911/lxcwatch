// The top bar: update badge, connection status and the settings menu.
import { Icon, ago, api, html, state, store, stored, update } from "./core.js";
import { openActivity } from "./actions.js";
import { openNotifications, openSettings } from "./dialogs.js";

const THEMES = ["system", "light", "dark"];
let theme = stored("theme", "system");
if (!THEMES.includes(theme)) theme = "system";

function applyTheme() {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}
applyTheme();

function setTheme(t) {
  theme = t;
  store("theme", t);
  applyTheme();
  update();
}

async function signOut() {
  try { await api("/api/logout", { method: "POST" }); } catch {}
  location.replace("/login");
}

function SelfUpdate() {
  const su = state.data?.self_update;
  return html`<a id="selfupdate" class="selfupdate" target="_blank" rel="noopener" hidden=${!su} href=${su?.url || undefined}
    title=${su ? `lxcwatch ${su.version} is out (you have ${su.current}). Upgrade with apt update && apt upgrade.` : undefined}>
    ${su && html`<span class="lbl-long">${su.version} available</span><span class="lbl-short">↑ ${su.version}</span>`}</a>`;
}

function Connection() {
  const conn = state.data?.connection;
  if (!conn) return html`<details class="conn" id="conn"><summary class="hbtn"><span class="dot"></span><span>Connecting…</span></summary></details>`;
  const online = conn.nodes.filter(n => n.online).length;
  const where = `${conn.user}@${conn.host}${conn.port !== 22 ? `:${conn.port}` : ""}`;
  const label = conn.ok === null ? `Connecting to ${where}…`
    : conn.ok ? `${conn.cluster || conn.host} · ${online}/${conn.nodes.length} node${conn.nodes.length === 1 ? "" : "s"}`
    : `Can't reach ${where}`;
  const dot = conn.ok === null ? "" : conn.ok && online === conn.nodes.length ? "ok" : "bad";
  const short = conn.ok === null ? "…" : conn.ok ? `${online}/${conn.nodes.length} nodes` : "offline";
  return html`<details class="conn" id="conn">
    <summary class="hbtn"><span class="dot ${dot}"></span><span class="conn-long">${label}</span><span class="conn-short">${short}</span></summary>
    <div class="pop">
      <div><b>Connected via</b><span>${where}</span></div>
      ${conn.cluster && html`<div><span class="muted">Cluster</span><span>${conn.cluster}</span></div>`}
      <div><span class="muted">Last contact</span><span>${conn.at ? ago(conn.at) : "never"}</span></div>
      ${conn.error && html`<div class="err small">${conn.error}</div>`}
      ${conn.nodes.length > 0 && html`<hr style="border:0;border-top:1px solid var(--border)" />`}
      ${conn.nodes.map(n => html`<div key=${n.name}><span><span class="dot ${n.online ? "ok" : "bad"}"></span> ${n.name}${n.local && html` <span class="muted small">(entry)</span>`}</span><span class="muted">${n.address}</span></div>`)}
    </div>
  </details>`;
}

const ThemeButton = ({ value, label, title, children }) => html`
  <button class=${theme === value ? "on" : ""} title=${title} onClick=${() => setTheme(value)}><${Icon}>${children}<//>${label}</button>`;

// No Sign out under Cloudflare Access: Access would sign the browser straight
// back in, and its logout ends the session for every Access app.
function Menu() {
  const v = state.data?.version, email = state.data?.access_email;
  return html`<details class="conn menu" id="menu">
    <summary class="hbtn icon" aria-label="Settings menu" title="Settings"><${Icon}><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/><//></summary>
    <div class="pop">
      <div class="menusec" id="accessrow" hidden=${!email}><span class="muted menulabel">Signed in through Cloudflare Access</span><span id="accessemail">${email || ""}</span></div>
      <div class="menusec"><span class="muted menulabel">Theme</span>
        <span class="seg wide" role="group" aria-label="Theme">
          <${ThemeButton} value="system" label="System" title="Follow the system"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/><//>
          <${ThemeButton} value="light" label="Light"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/><//>
          <${ThemeButton} value="dark" label="Dark"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/><//>
        </span></div>
      <div class="menurow"><span class="muted">Version <span id="version">${!state.data ? "—" : v && v !== "dev" ? v : "development build"}</span></span>${" "}
        <a href="https://github.com/devinsmith911/lxcwatch/releases" target="_blank" rel="noopener" class="ext">Release notes</a></div>
      <hr />
      <button id="settingsbtn" class="menuitem" onClick=${openSettings}><${Icon}><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M3 21v-5h5"/><//>Automatic updates</button>
      <button id="notifybtn" class="menuitem" onClick=${openNotifications}><${Icon}><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><//>Notifications</button>
      ${!email && html`<button id="logoutbtn" class="menuitem" onClick=${signOut}><${Icon}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/><//>Sign out</button>`}
    </div>
  </details>`;
}

function AccessBadge() {
  const email = state.data?.access_email;
  return html`<span id="accessbadge" class="hbtn accessbadge" hidden=${!email} title=${email ? `Signed in as ${email} through Cloudflare Access` : undefined}><${Icon}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/><//><span class="hlabel">Cloudflare Access</span></span>`;
}

export const Header = () => html`<header class="hero">
  <div class="brand">
    <img src="/icon.svg" alt="" width="36" height="36" />
    <h1>lxcwatch</h1>
    <${SelfUpdate} />
  </div>
  <div class="actions">
    <${AccessBadge} />
    <${Connection} />
    <button id="activitybtn" class="hbtn" title="Activity: recent jobs across all containers" onClick=${openActivity}><${Icon}><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 7v5l3 2"/><//><span class="hlabel">Activity</span></button>
    <${Menu} />
  </div>
</header>`;
