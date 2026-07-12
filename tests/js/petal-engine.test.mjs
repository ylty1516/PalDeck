import test from "node:test";
import assert from "node:assert/strict";

import {
  STYLE_PROFILES,
  createParticles,
  desiredCount,
  stepParticles,
} from "../../frontend/petal-engine.js";
import {
  createNaturalSpriteAtlas,
  createPetalUpdateCache,
  createWatercolorSpriteSet,
  naturalPalette,
  renderMinimalPetal,
  renderNaturalPetal,
  renderWatercolorPetal,
  watercolorSpriteKind,
} from "../../frontend/effects.js";
import { createRevisionGuard, createSerialQueue } from "../../frontend/interaction-policy.js";

const viewport = { width: 1200, height: 800 };

function sequenceRandom(values = [0.05, 0.4, 0.8, 0.2, 0.6, 0.95]) {
  let index = 0;
  return () => values[index++ % values.length];
}

function assertFiniteParticle(particle) {
  for (const [key, value] of Object.entries(particle)) {
    if (typeof value === "number") {
      assert.ok(Number.isFinite(value), `${key} must be finite`);
    }
  }
}

test("style profiles expose distinct motion and three depth layers", () => {
  assert.deepEqual(Object.keys(STYLE_PROFILES).sort(), ["minimal", "natural", "watercolor"]);
  assert.equal(STYLE_PROFILES.minimal.gust, 0.2);
  assert.notEqual(STYLE_PROFILES.natural.speed, STYLE_PROFILES.watercolor.speed);

  for (const style of Object.keys(STYLE_PROFILES)) {
    const particles = createParticles({
      style,
      level: "high",
      ...viewport,
      random: sequenceRandom(),
    });
    assert.deepEqual([...new Set(particles.map(({ depth }) => depth))].sort(), [0, 1, 2]);
    particles.forEach(assertFiniteParticle);
  }
});

test("density counts are fixed per style and minimal high stays at most 24", () => {
  for (const style of ["natural", "watercolor", "minimal"]) {
    assert.equal(desiredCount(style, "off"), 0);
    assert.ok(desiredCount(style, "low") < desiredCount(style, "medium"));
    assert.ok(desiredCount(style, "medium") < desiredCount(style, "high"));
    assert.equal(
      createParticles({ style, level: "high", ...viewport, random: () => 0.5 }).length,
      desiredCount(style, "high"),
    );
  }
  assert.ok(desiredCount("minimal", "high") <= 24);
  for (const inheritedKey of ["constructor", "toString", "__proto__", "length"]) {
    assert.throws(() => desiredCount(inheritedKey, "high"), /style/i);
    assert.throws(() => desiredCount("natural", inheritedKey), /level/i);
  }
  assert.throws(() => desiredCount("unknown", "high"), /style/i);
  assert.throws(() => desiredCount("natural", "many"), /level/i);
});

test("long frame delta is clamped to 0.04 seconds without mutating input", () => {
  const particles = createParticles({
    style: "natural", level: "low", ...viewport, random: sequenceRandom(),
  });
  const snapshot = structuredClone(particles);

  const longFrame = stepParticles(particles, {
    style: "natural", delta: 4, windTime: 1, ...viewport, random: () => 0.5,
  });
  const clampedFrame = stepParticles(particles, {
    style: "natural", delta: 0.04, windTime: 1, ...viewport, random: () => 0.5,
  });

  assert.deepEqual(longFrame, clampedFrame);
  assert.deepEqual(particles, snapshot);
  longFrame.forEach(assertFiniteParticle);
});

test("wind, rotation and flip evolve over a step", () => {
  const [particle] = createParticles({
    style: "watercolor", level: "low", ...viewport, random: () => 0.5,
  });
  const [next] = stepParticles([particle], {
    style: "watercolor", delta: 0.04, windTime: 9, ...viewport, random: () => 0,
  });

  assert.notEqual(next.x, particle.x);
  assert.notEqual(next.rotation, particle.rotation);
  assert.notEqual(next.flip, particle.flip);
  assert.ok(next.age > particle.age);
});

test("eligible particles receive a real gust contribution", () => {
  const [particle] = createParticles({
    style: "natural", level: "low", ...viewport, random: () => 0.5,
  });
  const windTime = Math.PI / (2 * 1.7);
  const options = { style: "natural", delta: 0.04, windTime, ...viewport, random: () => 0.5 };

  const [gusted] = stepParticles([{ ...particle, gustFactor: 0 }], options);
  const [notGusted] = stepParticles([{ ...particle, gustFactor: 0.5 }], options);

  assert.ok(gusted.x > notGusted.x);
});

test("every non-finite particle field triggers safe deterministic respawn", () => {
  const [particle] = createParticles({
    style: "natural", level: "low", ...viewport, random: () => 0.5,
  });
  const numericFields = [
    "x", "y", "depth", "size", "opacity", "blur", "vx", "vy", "drift",
    "driftPhase", "driftRate", "rotation", "rotationSpeed", "flipPhase",
    "flipSpeed", "flip", "gustFactor", "age", "lifetime", "lane",
  ];

  for (const [index, field] of numericFields.entries()) {
    const invalid = { ...particle, [field]: index % 2 ? Infinity : Number.NaN };
    const [recycled] = stepParticles([invalid], {
      style: "natural", delta: 0.04, windTime: 2, ...viewport, random: () => 0.25,
    });
    assert.equal(recycled.age, 0, `${field} should cause respawn`);
    assert.ok(recycled.y < 0, `${field} should respawn above viewport`);
    assertFiniteParticle(recycled);
  }
});

test("particle past lifetime recycles while still inside viewport", () => {
  const [particle] = createParticles({
    style: "minimal", level: "low", ...viewport, random: () => 0.5,
  });
  const expired = { ...particle, age: particle.lifetime };

  const [recycled] = stepParticles([expired], {
    style: "minimal", delta: 0.04, windTime: 2, ...viewport, random: () => 0.25,
  });

  assert.ok(recycled.y < 0);
  assert.equal(recycled.age, 0);
  assertFiniteParticle(recycled);
});

test("particle outside viewport recycles before its lifetime expires", () => {
  const [particle] = createParticles({
    style: "minimal", level: "low", ...viewport, random: () => 0.5,
  });
  const outside = { ...particle, y: viewport.height + 200, age: 0 };

  const [recycled] = stepParticles([outside], {
    style: "minimal", delta: 0.04, windTime: 2, ...viewport, random: () => 0.25,
  });

  assert.ok(recycled.y < 0);
  assert.equal(recycled.age, 0);
  assertFiniteParticle(recycled);
});

test("minimal particles choose and retain a safety lane without center projection", () => {
  const particles = createParticles({
    style: "minimal", level: "high", ...viewport, random: sequenceRandom(),
  });
  const edge = viewport.width * 0.2;
  assert.ok(particles.every(({ lane, x }) =>
    (lane === -1 && x >= 0 && x <= edge) || (lane === 1 && x >= viewport.width - edge && x <= viewport.width),
  ));

  const left = { ...particles.find(({ lane }) => lane === -1), x: edge - 0.01, vx: 1000 };
  const [next] = stepParticles([left], {
    style: "minimal", delta: 0.04, windTime: 20, ...viewport, random: () => 0.5,
  });
  assert.equal(next.lane, -1);
  assert.ok(next.x >= left.x && next.x <= edge);
  assert.ok(Math.abs(next.x - left.x) < 1, "crossing the lane edge must clamp, not jump across the center");
});

test("petal update cache only rebuilds particles when level or style changes", () => {
  let particles = Object.freeze([{ identity: "original" }]);
  let calls = 0;
  const cache = createPetalUpdateCache(({ level, style }) => {
    calls += 1;
    particles = Object.freeze([{ identity: `${level}:${style}` }]);
  });
  const original = particles;
  assert.equal(cache.update({ level: "off", style: "natural" }), false);
  assert.equal(calls, 0);
  assert.equal(particles, original);
  assert.equal(cache.update({ level: "medium", style: "natural" }), true);
  const medium = particles;
  assert.equal(cache.update({ level: "medium", style: "natural", mask: 0.7, blur: 8, position: "top" }), false);
  assert.equal(calls, 1);
  assert.equal(particles, medium);
  assert.equal(cache.update({ level: "medium", style: "watercolor" }), true);
  assert.equal(calls, 2);
});

test("revision guard rejects late save responses after a newer preview", async () => {
  const revisions = createRevisionGuard();
  const saveRevision = revisions.capture();
  const lateResponse = Promise.resolve("server-old-style");
  revisions.bump();
  let applied = "new-preview";
  const response = await lateResponse;
  assert.equal(revisions.apply(saveRevision, () => { applied = response; }), false);
  assert.equal(applied, "new-preview");
  const currentRevision = revisions.capture();
  assert.equal(revisions.apply(currentRevision, () => { applied = "server-current"; }), true);
  assert.equal(applied, "server-current");
});

test("background writes complete in request order and queue survives failure", async () => {
  const queue = createSerialQueue();
  const events = [];
  let releaseFirst;
  const gate = new Promise((resolve) => { releaseFirst = resolve; });
  const first = queue.enqueue(async () => {
    events.push("reset:start");
    await gate;
    events.push("reset:finish");
  });
  const second = queue.enqueue(async () => {
    events.push("upload:start");
    events.push("upload:finish");
  });
  await Promise.resolve();
  assert.deepEqual(events, ["reset:start"]);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(events, ["reset:start", "reset:finish", "upload:start", "upload:finish"]);

  await assert.rejects(queue.enqueue(async () => { throw new Error("write failed"); }), /write failed/);
  await queue.enqueue(async () => { events.push("after-failure"); });
  assert.equal(events.at(-1), "after-failure");
});

test("successful queued background writes always refresh but never replace a newer preview", async () => {
  const queue = createSerialQueue();
  const revisions = createRevisionGuard();
  const sideEffects = [];
  let appearance = "original";
  const complete = (revision, saved, label) => {
    revisions.apply(revision, () => { appearance = saved; });
    sideEffects.push(`${label}:refresh`, `${label}:success`);
  };
  const resetRevision = revisions.bump();
  const reset = queue.enqueue(async () => complete(resetRevision, "reset-server", "reset"));
  const uploadRevision = revisions.bump();
  const upload = queue.enqueue(async () => complete(uploadRevision, "upload-server", "upload"));
  revisions.bump();
  appearance = "new-mask-style-preview";
  await Promise.all([reset, upload]);
  assert.equal(appearance, "new-mask-style-preview");
  assert.deepEqual(sideEffects, ["reset:refresh", "reset:success", "upload:refresh", "upload:success"]);
});

function recordingContext() {
  const calls = [];
  const gradient = { addColorStop: (...args) => calls.push(["addColorStop", ...args]) };
  const context = new Proxy({ calls, canvas: { width: 48, height: 48 } }, {
    get(target, key) {
      if (key in target) return target[key];
      if (key === "createLinearGradient" || key === "createRadialGradient") {
        return (...args) => { calls.push([key, ...args]); return gradient; };
      }
      return (...args) => calls.push([key, ...args]);
    },
    set(target, key, value) { calls.push(["set", key, value]); target[key] = value; return true; },
  });
  return context;
}

const renderedParticle = Object.freeze({
  x: 20, y: 30, size: 10, rotation: 0.4, flip: -0.5, opacity: 0.7, blur: 1.2,
  gustFactor: 0.32, drift: 20, lifetime: 14,
});

test("natural atlas owns local gradients while the transformed renderer reuses sprites", () => {
  const offscreen = [];
  const atlas = createNaturalSpriteAtlas({
    createCanvas: () => {
      const context = recordingContext();
      const canvas = { width: 0, height: 0, getContext: () => context };
      offscreen.push({ canvas, context });
      return canvas;
    },
  });
  assert.equal(atlas.length, 12);
  assert.equal(offscreen.length, 12);
  assert.ok(offscreen.every(({ context }) => context.calls.some(([name]) => name === "createLinearGradient")));
  assert.ok(offscreen.every(({ context }) => context.calls.filter(([name]) => name === "quadraticCurveTo").length === 2));
  assert.ok(offscreen.every(({ context }) => context.calls.some(([name]) => name === "stroke")));

  const context = recordingContext();
  renderNaturalPetal(context, renderedParticle, atlas);
  renderNaturalPetal(context, { ...renderedParticle, rotation: 1.2, flip: 0.4 }, atlas);
  const names = context.calls.map(([name]) => name);
  assert.equal(names.filter((name) => name === "createLinearGradient").length, 0);
  assert.equal(names.filter((name) => name === "drawImage").length, 2);
  const images = context.calls.filter(([name]) => name === "drawImage").map((call) => call[1]);
  assert.equal(images[0], images[1]);
  assert.ok(context.calls.some((call) => call[0] === "scale" && call[2] === renderedParticle.flip));
  assert.ok(context.calls.some((call) => call[0] === "set" && call[1] === "filter"));

  const firstPalette = naturalPalette(renderedParticle);
  assert.deepEqual(naturalPalette({ ...renderedParticle, rotation: 2.8, flip: 0.2 }), firstPalette);
  assert.notDeepEqual(naturalPalette({ ...renderedParticle, gustFactor: 0.9 }), firstPalette);
  for (const color of [firstPalette.highlight, firstPalette.middle, firstPalette.edge]) {
    const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number);
    assert.ok(channels[0] >= 180 && channels[0] <= 255);
    assert.ok(channels[1] >= 85 && channels[1] <= 248);
    assert.ok(channels[2] >= 125 && channels[2] <= 248);
  }
});

test("watercolor sprites are finite, pre-generated and guarantee a reachable atlas", () => {
  const canvases = [];
  const sprites = createWatercolorSpriteSet({
    count: 6,
    random: sequenceRandom([0.02, 0.4, 0.8, 0.6]),
    createCanvas: () => {
      const context = recordingContext();
      const canvas = { width: 0, height: 0, getContext: () => context };
      canvases.push({ canvas, context });
      return canvas;
    },
  });
  assert.equal(sprites.length, 6);
  assert.equal(canvases.length, 6);
  assert.equal(sprites.filter(({ kind }) => kind === "bloom").length, 1);
  assert.ok(sprites.filter(({ kind }) => kind === "petal").length >= 2);
  assert.ok(canvases.some(({ context }) => context.calls.filter(([name]) => name === "rotate").length >= 5));

  const target = recordingContext();
  renderWatercolorPetal(target, { ...renderedParticle, gustFactor: 0.02 }, sprites);
  assert.equal(target.calls.find(([name]) => name === "drawImage")[1], sprites.find(({ kind }) => kind === "bloom").image);
});

test("watercolor bloom choice is stable per birth and stays within 5-8 percent", () => {
  assert.equal(watercolorSpriteKind({ gustFactor: 0.0649 }), "bloom");
  assert.equal(watercolorSpriteKind({ gustFactor: 0.065 }), "petal");
  const samples = Array.from({ length: 10000 }, (_, index) => ({ gustFactor: index / 10000 }));
  const bloomCount = samples.filter((particle) => watercolorSpriteKind(particle) === "bloom").length;
  assert.ok(bloomCount >= 500 && bloomCount <= 800);
  assert.equal(watercolorSpriteKind({ ...renderedParticle, rotation: 5, flip: 0.1 }), watercolorSpriteKind(renderedParticle));
});

test("minimal renderer uses a low-saturation solid silhouette", () => {
  const context = recordingContext();
  renderMinimalPetal(context, renderedParticle);
  assert.ok(context.calls.some((call) => call[0] === "set" && call[1] === "fillStyle" && /rgba\(190, 178, 184/.test(call[2])));
  assert.ok(context.calls.some(([name]) => name === "bezierCurveTo"));
});
