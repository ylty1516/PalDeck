import test from "node:test";
import assert from "node:assert/strict";

import {
  STYLE_PROFILES,
  createParticles,
  desiredCount,
  stepParticles,
} from "../../frontend/petal-engine.js";
import {
  createWatercolorSpriteSet,
  minimalSafeX,
  renderMinimalPetal,
  renderNaturalPetal,
  renderWatercolorPetal,
} from "../../frontend/effects.js";

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
    "flipSpeed", "flip", "gustFactor", "age", "lifetime",
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

test("minimal projection keeps generation and drift in fixed edge bands", () => {
  const edge = viewport.width * 0.2;
  for (const x of [-50, 0, 120, 300, 599, 600, 880, 1199, 1350]) {
    const projected = minimalSafeX(x, viewport.width);
    assert.ok(projected >= 0 && projected <= viewport.width);
    assert.ok(projected <= edge || projected >= viewport.width - edge);
  }
  assert.throws(() => minimalSafeX(20, 0), /width/i);
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
});

test("natural renderer uses asymmetric bezier geometry, gradient, vein and depth effects", () => {
  const context = recordingContext();
  renderNaturalPetal(context, renderedParticle);
  const names = context.calls.map(([name]) => name);
  assert.ok(names.includes("bezierCurveTo"));
  assert.ok(names.includes("createLinearGradient"));
  assert.ok(names.includes("stroke"));
  assert.ok(context.calls.some((call) => call[0] === "set" && call[1] === "shadowBlur"));
  assert.ok(context.calls.some((call) => call[0] === "scale" && call[2] === renderedParticle.flip));

  const rotated = recordingContext();
  renderNaturalPetal(rotated, { ...renderedParticle, rotation: 1.4 });
  assert.notDeepEqual(
    context.calls.filter(([name]) => name === "bezierCurveTo")[0],
    rotated.calls.filter(([name]) => name === "bezierCurveTo")[0],
  );
});

test("watercolor sprites are finite, pre-generated and support layered five-petal blooms", () => {
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
  assert.ok(canvases.some(({ context }) => context.calls.filter(([name]) => name === "rotate").length >= 5));

  const target = recordingContext();
  renderWatercolorPetal(target, renderedParticle, sprites, 8);
  assert.ok(target.calls.some(([name]) => name === "drawImage"));
});

test("minimal renderer uses a low-saturation solid silhouette", () => {
  const context = recordingContext();
  renderMinimalPetal(context, renderedParticle);
  assert.ok(context.calls.some((call) => call[0] === "set" && call[1] === "fillStyle" && /rgba\(190, 178, 184/.test(call[2])));
  assert.ok(context.calls.some(([name]) => name === "bezierCurveTo"));
});
