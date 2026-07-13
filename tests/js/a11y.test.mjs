import test from "node:test";
import assert from "node:assert/strict";

import { rememberFocus, restoreFocus, setExpanded } from "../../frontend/a11y.js";

test("expanded state synchronizes aria and hidden", () => {
  const values = new Map();
  const button = { setAttribute(name, value) { values.set(name, value); } };
  const panel = { hidden: true };
  setExpanded(button, panel, true);
  assert.equal(values.get("aria-expanded"), "true");
  assert.equal(panel.hidden, false);
});

test("focus returns only to a connected focusable trigger", () => {
  let focused = 0;
  const trigger = { isConnected: true, focus() { focused += 1; } };
  rememberFocus({ activeElement: trigger });
  assert.equal(restoreFocus(), true);
  assert.equal(focused, 1);
  assert.equal(restoreFocus(), false);
});
