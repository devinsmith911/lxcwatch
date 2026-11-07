// Shared by everything: the view library, app state and small helpers.
import { html } from "/vendor/preact-htm.js";
export { html };
export { render, useEffect, useLayoutEffect, useRef, useState } from "/vendor/preact-htm.js";

export function stored(key, fallback) {
  try { const v = localStorage.getItem(`lxcwatch.${key}`); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
export function store(key, value) {
  try { localStorage.setItem(`lxcwatch.${key}`, JSON.stringify(value)); } catch {}
}

// Table columns, in display order: [key, label, shown by default]. The select
// box and the actions are always there.
export const COLUMNS = [
  ["container", "Container", true],
  ["node", "Node", true],
  ["tags", "Tags", true],
  ["app", "App", true],
  ["packages", "Packages", true],
  ["auto", "Auto updates", true],
  ["uptime", "Uptime", false],
  ["scanned", "Scanned", true],
];
export const DEFAULT_COLUMNS = COLUMNS.filter(([, , on]) => on).map(([k]) => k);
function storedColumns() {
  const v = stored("columns", null);
  return Array.isArray(v) ? v.filter(k => COLUMNS.some(([c]) => c === k)) : DEFAULT_COLUMNS;
}

export const state = {
  data: null, error: null, filter: "all", q: "", selected: new Set(), drawer: null, dialog: null,
  page: 1, pageSize: stored("pageSize", 10), hideStopped: stored("hideStopped", false), hideNonScript: stored("hideNonScript", false),
  columns: storedColumns(),
  sort: stored("sort", null),  // {key, dir} or null for the server's order
  bulkSnapshot: false, batchStopping: false, activityFilter: "all",
};

// Views read `state` directly; call update() after changing it.
let redraw = () => {};
export const onUpdate = fn => { redraw = fn; };
export const update = () => redraw();

function toLogin() {
  location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
}

export async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Requested-With": "lxcwatch", "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (r.status === 401) { toLogin(); throw new Error("Signed out"); }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || r.statusText);
  return body;
}
export const post = (path, body) => api(path, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) });

export function duration(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`;
}

export function until(ts) {
  const s = ts - Date.now() / 1000;
  if (s < 90) return "any moment";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  if (s < 86400) return `in ${Math.round(s / 3600)}h`;
  return `in ${Math.round(s / 86400)}d`;
}

export function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const fmtWhen = t => new Date(t * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export const KIND_LABEL = { app: "app update", os: "package update", release: "release upgrade", restart: "restart", rollback: "rollback", snapshot: "snapshot" };
export const STEP_BADGE = { pending: "", running: "warn", success: "ok", failed: "bad", skipped: "info" };
export const jobBadge = status => status === "success" ? "ok" : status === "failed" ? "bad" : "warn";

export const AUTO_POLICIES = [
  ["off", "Off", "Off", "Nothing is installed automatically."],
  ["security", "Security", "Security packages", "Only package updates from security repos."],
  ["packages", "Packages", "Packages", "All pending package updates."],
  ["app", "App + packages", "App and packages", "The app update, then all package updates."],
];
export const AUTO_NAME = Object.fromEntries(AUTO_POLICIES.map(([k, , long]) => [k, long]));

// Rules for reading a container record.
export const needsUpdate = c => c.outdated > 0 || c.pkg_count > 0;
// Scanned fine, but there's no community-scripts `update` in it.
export const notScript = c => !!c.os && !c.script;
// A community-script (or not yet scanned) container whose app version we can't tell.
export const isUnknown = c => c.status === "running" && !notScript(c) && (!c.scanned_at || !c.os
  || (c.components.length > 0 && c.components.every(x => x.state === "unknown" || x.state === "untracked")));
export const rowState = c => c.status !== "running" ? "stopped" : c.pkg_security ? "security" : needsUpdate(c) ? "outdated" : isUnknown(c) || notScript(c) || c.pkg_stale ? "unknown" : "current";
// community-scripts reports removed scripts as state "deleted" or found: false with a message.
export const retired = c => !!c.script_state && (c.script_state.state === "deleted" || (c.script_state.found === false && !!c.script_state.message));
export const problem = c => c.error || c.failures?.some(f => !f.superseded) || c.upstream_errors.length || c.components.some(x => x.pending) || retired(c) || c.last_job?.status === "failed" || c.pkg_stale;
export const appPending = c => c.status === "running" && c.script && c.outdated > 0;
export const osPending = c => c.status === "running" && c.pkgmgr && c.pkg_count > 0;
export const container = vmid => state.data.containers.find(c => c.vmid === vmid);

export const Ext = ({ href, class: cls = "ext", children, title }) => html`<a class=${cls} href=${href} target="_blank" rel="noopener" title=${title}>${children}</a>`;
export const Icon = ({ children }) => html`<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${children}</svg>`;
