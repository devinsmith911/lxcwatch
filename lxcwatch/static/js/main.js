// Entry point: draws the page and keeps it up to date.
import { html, onUpdate, render, state } from "./core.js";
import { closeDrawer, refresh } from "./actions.js";
import { Dialogs } from "./dialogs.js";
import { Drawer } from "./drawer.js";
import { Header } from "./header.js";
import { Shell } from "./shell.js";
import { Dashboard, closePkgs } from "./table.js";

const root = document.querySelector("#app");
const draw = () => render(html`<${Header} /><${Dashboard} /><${Drawer} /><${Dialogs} /><${Shell} />`, root);
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
  if (state.pkgPop && !e.target.closest(".pkgpop, .pkgbadge")) closePkgs();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && state.pkgPop) return closePkgs();
  if (e.key === "Escape" && state.drawer && !document.querySelector("#confirm[open]")) closeDrawer();
});
// The package popover is placed against its badge, so it can't follow a scroll.
addEventListener("scroll", e => { if (state.pkgPop && !e.target.closest?.(".pkgpop")) closePkgs(); }, true);

refresh();
setInterval(refresh, 5000);
