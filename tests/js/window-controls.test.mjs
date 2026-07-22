import test from "node:test";
import assert from "node:assert/strict";

import { callWindowControl, initializeWindowControls, setupWindowControls } from "../../frontend/window-controls.js";

test("window controls fail closed outside pywebview and reject unknown calls", async () => {
  assert.equal(await callWindowControl("minimize", {}), false);
  assert.equal(await callWindowControl("execute", { pywebview: { api: { execute() {} } } }), false);
});

test("allowed controls wait for a late bridge and invoke only the fixed method", async () => {
  const calls = [];
  const host = { location: { search: "?chrome=1" } };
  setTimeout(() => { host.pywebview = { api: { minimize: async () => calls.push("minimize") } }; }, 10);
  assert.equal(await callWindowControl("minimize", host), true);
  assert.deepEqual(calls, ["minimize"]);
});

test("query contract reveals custom chrome synchronously even if pywebviewready already fired", async () => {
  const classes = new Set();
  const chrome = { hidden: true };
  const root = {
    documentElement: { classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); } } },
    querySelector(selector) { return selector === ".window-chrome" ? chrome : null; },
    addEventListener() {},
  };
  const host = { location: { search: "?chrome=1" } };
  await initializeWindowControls(root, host);
  assert.equal(chrome.hidden, false);
  assert.ok(classes.has("has-custom-chrome"));
});

test("setup reveals custom chrome only when bridge reports it", async () => {
  const classes = new Set();
  const chrome = { hidden: true };
  const root = {
    documentElement: { classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); } } },
    querySelector(selector) { return selector === ".window-chrome" ? chrome : null; },
    addEventListener() {},
  };
  const host = { pywebview: { api: { get_state: async () => ({ custom_chrome: true }) } } };
  assert.equal(await setupWindowControls(root, host), true);
  assert.equal(chrome.hidden, false);
  assert.ok(classes.has("has-custom-chrome"));
});
