// A root shell in a container, in a terminal over the page. xterm.js is only
// loaded the first time one opens.
import { Icon, container, html, state, update, useEffect, useRef, useState } from "./core.js";

let xterm = null;
function loadTerminal() {
  if (!document.querySelector("#xterm-css")) {
    const link = document.createElement("link");
    link.id = "xterm-css";
    link.rel = "stylesheet";
    link.href = "/vendor/xterm.css";
    document.head.append(link);
  }
  xterm ||= Promise.all([import("/vendor/xterm.js"), import("/vendor/xterm-addon-fit.js")])
    .catch(e => { xterm = null; throw e; });
  return xterm;
}

let shellKey = 0;
export function openShell(vmid) {
  state.shell = { vmid, key: ++shellKey };
  update();
}
export function closeShell() {
  state.shell = null;
  update();
}

// The terminal is dark in both themes, like the job log.
const THEME = { background: "#0b0e12", foreground: "#d8dee6", cursor: "#d8dee6", selectionBackground: "#3a4252" };
const STATUS = { connecting: ["warn", "connecting…"], open: ["ok", "connected"], ended: ["", "ended"], failed: ["bad", "failed"] };

function Terminal({ sh }) {
  const box = useRef();
  const [status, setStatus] = useState({ s: "connecting" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let ws, term, observer, keepalive, gone = false;
    const say = (text, colour = "33") => term?.write(`\r\n\x1b[${colour}m${text}\x1b[0m\r\n`);
    loadTerminal().then(([{ Terminal: Term }, { FitAddon }]) => {
      if (gone) return;
      term = new Term({ theme: THEME, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 13, cursorBlink: true, scrollback: 5000 });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(box.current);
      fit.fit();
      term.focus();
      const q = new URLSearchParams({ cols: term.cols, rows: term.rows });
      ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/containers/${sh.vmid}/shell?${q}`);
      ws.binaryType = "arraybuffer";
      const enc = new TextEncoder();
      const send = data => ws.readyState === WebSocket.OPEN && ws.send(data);
      let opened = false, ended = false;
      ws.onopen = () => { opened = true; setStatus({ s: "open" }); };
      ws.onmessage = ev => {
        if (typeof ev.data !== "string") return term.write(new Uint8Array(ev.data));
        const m = JSON.parse(ev.data);
        ended = true;
        if (m.error) { say(m.error, "31"); setStatus({ s: "failed" }); }
        else { say(`Session ended${m.exit ? ` (exit ${m.exit})` : ""}.`, "90"); setStatus({ s: "ended" }); }
      };
      ws.onclose = ev => {
        if (gone || ended) return;
        say(opened ? "Disconnected." : "Couldn't open a shell. Are you still signed in?", "31");
        setStatus({ s: "failed" });
      };
      term.onData(d => send(enc.encode(d)));
      term.onBinary(d => send(Uint8Array.from(d, ch => ch.charCodeAt(0))));
      term.onResize(({ cols, rows }) => send(JSON.stringify({ resize: [cols, rows] })));
      observer = new ResizeObserver(() => fit.fit());
      observer.observe(box.current);
      // Some proxies drop WebSockets that are quiet for a minute or two.
      keepalive = setInterval(() => send("{}"), 30000);
    }).catch(e => {
      setStatus({ s: "failed" });
      box.current.textContent = `Couldn't load the terminal: ${e.message}`;
    });
    return () => {
      gone = true;
      clearInterval(keepalive);
      observer?.disconnect();
      ws?.close();
      term?.dispose();
    };
  }, [attempt]);
  const [cls, text] = STATUS[status.s];
  return html`<div class="shellbar">
      <span class="badge ${cls}">${text}</span>
      ${(status.s === "ended" || status.s === "failed") && html`<button onClick=${() => { setStatus({ s: "connecting" }); setAttempt(n => n + 1); }}>Reconnect</button>`}
    </div>
    <div class="term" ref=${box} key=${attempt}></div>`;
}

export function Shell() {
  const sh = state.shell;
  if (!sh) return null;
  const c = state.data && container(sh.vmid);
  // Keys go to the terminal: Escape here mustn't close the drawer underneath.
  return html`<div class="shellwrap" role="dialog" aria-modal="true" aria-label="Shell" onKeyDown=${e => e.stopPropagation()}>
    <div class="shellpanel">
      <header>
        <div><h1>Shell${c ? ` · ${c.vmid} ${c.name}` : ` · CT ${sh.vmid}`}</h1>
          <div class="muted small">Root shell${c ? ` on ${c.node}` : ""} (<code>pct enter ${sh.vmid}</code>). Closing this ends the session.</div></div>
        <button class="xbtn" aria-label="Close" title="Close" onClick=${closeShell}><${Icon}><path d="M6 6l12 12M18 6L6 18"/><//></button>
      </header>
      <${Terminal} key=${sh.key} sh=${sh} />
    </div>
  </div>`;
}
