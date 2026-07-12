import test from "node:test";
import assert from "node:assert/strict";

import {
  STYLE_PROFILES,
  createParticles,
  desiredCount,
  stepParticles,
} from "../../frontend/petal-engine.js";

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
