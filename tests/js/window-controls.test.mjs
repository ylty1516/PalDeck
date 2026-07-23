import test from "node:test";
import assert from "node:assert/strict";

import {
  callWindowControl, initializeWindowControls, setupNativeWindowDrag, setupWindowControls,
} from '../../frontend/window-controls.js';

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

test('native drag replaces the high-frequency pywebview mousemove bridge', async () => {
  const listeners = new Map();
  const classes = new Set(['pywebview-drag-region']);
  const region = {
    classList: { remove(name) { classes.delete(name); } },
    addEventListener(name, handler) { listeners.set(name, handler); },
  };
  const calls = [];
  const root = {
    querySelector(selector) { return selector === '.window-drag-region' ? region : null; },
  };
  const host = { pywebview: { api: {
    begin_drag: async () => calls.push('begin_drag'),
    toggle_maximize: async () => calls.push('toggle_maximize'),
  } } };
  assert.equal(setupNativeWindowDrag(root, host), true);
  assert.equal(classes.has('pywebview-drag-region'), false);
  listeners.get('mousedown')({ button: 0, detail: 1, preventDefault() {} });
  listeners.get('dblclick')({ button: 0, preventDefault() {} });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, ['begin_drag', 'toggle_maximize']);
});
