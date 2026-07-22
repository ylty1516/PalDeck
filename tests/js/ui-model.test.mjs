import test from "node:test";
import assert from "node:assert/strict";

import {
  createImportQueue,
  deriveModView,
  normalizedModState,
  reduceImportQueue,
} from "../../frontend/ui-model.js";

const mods = Object.freeze([
  Object.freeze({ id: "local-1", name: "Better Pal", mod_type: "pak", nexus_id: 101, author: "Aiko", status: "enabled", source: "local" }),
  Object.freeze({ id: "local-2", name: "Script Tools", mod_type: "ue4ss", nexus_id: 202, author: "Ren", audit: Object.freeze({ status: "disabled" }), source: "nexus" }),
  Object.freeze({ workshop_id: "334455", name: "Workshop Map", install_types: Object.freeze(["logicpak"]), author: "Mori", enabled: true, valid: true, source: "steam_workshop" }),
  Object.freeze({ id: "broken", name: "Missing Assets", mod_type: "pak", author: "Kai", status: "missing" }),
]);

test("normalizedModState only accepts explicit local states and valid Workshop mapping", () => {
  assert.equal(normalizedModState(mods[0]), "enabled");
  assert.equal(normalizedModState(mods[1]), "disabled");
  assert.equal(normalizedModState(mods[2]), "enabled");
  assert.equal(normalizedModState({ source: "steam_workshop", enabled: false, valid: true }), "disabled");
  assert.equal(normalizedModState({ source: "steam_workshop", enabled: false, valid: false }), "abnormal");
  assert.equal(normalizedModState({ status: "corrupt", enabled: true }), "abnormal");
  assert.equal(normalizedModState({ status: "missing" }), "abnormal");
  assert.equal(normalizedModState({ enabled: false }), "abnormal");
});

test("deriveModView reports totals and filters all specified searchable fields", () => {
  const all = deriveModView(mods, {});
  assert.deepEqual(all.stats, { total: 4, enabled: 2, disabled: 1, abnormal: 1 });
  assert.deepEqual(all.items, mods);

  for (const [query, expected] of [
    ["better", "local-1"], ["UE4SS", "local-2"], ["101", "local-1"],
    ["334455", "334455"], ["mori", "334455"],
  ]) {
    const view = deriveModView(mods, { query });
    assert.equal(view.items.length, 1, query);
    assert.equal(String(view.items[0].id ?? view.items[0].workshop_id), expected);
  }
});

test("deriveModView normalizes missing source to local and supports all or empty filters", () => {
  const snapshot = structuredClone(mods);
  const localAbnormal = deriveModView(mods, { source: "local", status: "abnormal", query: "pak" });
  assert.deepEqual(localAbnormal.items.map(({ id }) => id), ["broken"]);
  assert.deepEqual(deriveModView(mods, { source: "steam_workshop", status: "enabled" }).items, [mods[2]]);
  assert.deepEqual(deriveModView(mods, { source: "all", status: "all" }).items, mods);
  assert.deepEqual(deriveModView(mods, { source: null, status: undefined }).items, mods);
  assert.deepEqual(mods, snapshot);
});

test("external filter distinguishes discovered mods while local keeps both local kinds", () => {
  const mods = [
    { id: "managed", source: "local", status: "enabled", externally_discovered: false },
    { id: "external", source: "local", status: "disabled", externally_discovered: true },
    { workshop_id: "9", source: "steam_workshop", valid: true, enabled: true },
  ];
  const external = deriveModView(mods, { source: "external" });
  const local = deriveModView(mods, { source: "local" });
  assert.deepEqual(external.items.map((item) => item.id), ["external"]);
  assert.deepEqual(local.items.map((item) => item.id), ["managed", "external"]);
  assert.equal(external.stats.total, 3);
  assert.equal(local.stats.total, 3);
});

test("deriveModView safely excludes malformed records", () => {
  const valid = { id: "ok", name: "Safe", source: "local", status: "enabled" };
  const result = deriveModView([null, [], "bad", 42, valid], { source: "local", query: "safe" });
  assert.deepEqual(result.items, [valid]);
  assert.deepEqual(result.stats, { total: 1, enabled: 1, disabled: 0, abnormal: 0 });
});

test("import queue transitions immutably through start and success", () => {
  const files = Object.freeze([Object.freeze({ id: "a", name: "a.zip" }), Object.freeze({ id: "b", name: "b.pak" })]);
  const initial = createImportQueue(files);
  assert.deepEqual(initial.map(({ id, status }) => ({ id, status })), [
    { id: "a", status: "queued" }, { id: "b", status: "queued" },
  ]);
  assert.notEqual(initial[0], files[0]);

  const installing = reduceImportQueue(initial, { type: "start", id: "a" });
  const complete = reduceImportQueue(installing, { type: "succeed", id: "a", result: { installed: true } });
  assert.equal(installing[0].status, "installing");
  assert.equal(complete[0].status, "succeeded");
  assert.deepEqual(complete[0].result, { installed: true });
  assert.equal(initial[0].status, "queued");
  assert.deepEqual(files, [{ id: "a", name: "a.zip" }, { id: "b", name: "b.pak" }]);
});

test("createImportQueue assigns stable unique ids across explicit and generated collisions", () => {
  const queue = createImportQueue([{ id: "2" }, {}, { id: "2" }, {}]);
  assert.deepEqual(queue.map(({ id }) => id), ["2", "2-2", "2-3", "4"]);
  assert.equal(new Set(queue.map(({ id }) => id)).size, queue.length);
});

test("conflict pauses later items and permits at most one active item", () => {
  let queue = createImportQueue([{ id: "a" }, { id: "b" }]);
  queue = reduceImportQueue(queue, { type: "start", id: "a" });
  assert.throws(() => reduceImportQueue(queue, { type: "start", id: "b" }), /active|installing|conflict/i);
  queue = reduceImportQueue(queue, { type: "conflict", id: "a", conflict: { paths: ["x.pak"] } });
  assert.equal(queue[0].status, "conflict");
  assert.throws(() => reduceImportQueue(queue, { type: "start", id: "b" }), /conflict|paused/i);
  assert.equal(queue.filter(({ status }) => ["installing", "conflict"].includes(status)).length, 1);
});

test("failed and cancelled imports can retry while invalid transitions fail", () => {
  let queue = createImportQueue([{ id: "a" }]);
  queue = reduceImportQueue(queue, { type: "start", id: "a" });
  queue = reduceImportQueue(queue, { type: "fail", id: "a", error: "bad archive" });
  assert.equal(queue[0].status, "failed");
  queue = reduceImportQueue(queue, { type: "retry", id: "a" });
  assert.equal(queue[0].status, "queued");
  queue = reduceImportQueue(queue, { type: "cancel", id: "a" });
  assert.equal(queue[0].status, "cancelled");
  queue = reduceImportQueue(queue, { type: "retry", id: "a" });
  assert.equal(queue[0].status, "queued");
  assert.throws(() => reduceImportQueue(queue, { type: "explode", id: "a" }), TypeError);
  assert.throws(() => reduceImportQueue(queue, { type: "succeed", id: "a" }), /transition/i);
});
