`preact-htm.js` is `preact/standalone.module.js` from the npm package htm 3.1.1, unmodified. It bundles Preact and its hooks with htm, so the dashboard needs no build step. To update it, replace the file with the same one from a newer htm release.

htm is Apache-2.0 and Preact is MIT; see `LICENSE-htm` and `LICENSE-preact`.

`xterm.js`, `xterm-addon-fit.js` and `xterm.css` are the shell's terminal: `lib/xterm.mjs` and `css/xterm.css` from the npm package @xterm/xterm 6.0.0, and `lib/addon-fit.mjs` from @xterm/addon-fit 0.11.0, renamed but otherwise unmodified. They're only loaded when a shell is opened. xterm.js is MIT; see `LICENSE-xterm`.
