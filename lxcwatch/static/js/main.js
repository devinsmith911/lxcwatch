// Entry point: draws the page and keeps it up to date.
import { html, onUpdate, render, state } from "./core.js";
import { closeDrawer, refresh } from "./actions.js";
import { Dialogs } from "./dialogs.js";
import { Drawer } from "./drawer.js";
import { Header } from "./header.js";
import { Dashboard } from "./table.js";

const root = document.querySelector("#app");
const draw = () => render(html`<${Header} /><${Dashboard} /><${Drawer} /><${Dialogs} />`, root);
// Several changes in one event only redraw once.
let queued = false;
onUpdate(() => {
  if (queued) return;
  queued = true;
  queueMicrotask(() => { queued = false; draw(); });
});
draw();

// Clicking elsewhere closes the popovers.
document.addEventListener("click", e => {
  for (const id of ["conn", "menu", "columns"]) {
    const el = document.querySelector(`#${id}`);
    if (el && !e.target.closest(`#${id}`)) el.open = false;
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && state.drawer && !document.querySelector("#confirm[open]")) closeDrawer();
});

refresh();
setInterval(refresh, 5000);
