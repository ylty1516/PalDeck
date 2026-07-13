import test from "node:test";
import assert from "node:assert/strict";

import { ICON_NAMES, iconDefinition } from "../../frontend/icons.js";

test("fixed icon catalog exposes required local line icons", () => {
  for (const name of ["mods", "import", "nexus", "settings", "credits", "folder", "refresh", "search"]) {
    assert.ok(ICON_NAMES.includes(name));
    const definition = iconDefinition(name);
    assert.equal(definition.viewBox, "0 0 24 24");
    assert.ok(definition.paths.length > 0);
    assert.ok(definition.paths.every((path) => typeof path === "string" && !/[<>]/.test(path)));
  }
});

test("unknown icons fail closed and definitions are immutable", () => {
  assert.equal(iconDefinition("unknown"), null);
  assert.equal(iconDefinition("<svg>"), null);
  assert.ok(Object.isFrozen(ICON_NAMES));
  assert.ok(Object.isFrozen(iconDefinition("mods")));
  assert.ok(Object.isFrozen(iconDefinition("mods").paths));
});
