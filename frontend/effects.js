import { createParticles, desiredCount, stepParticles } from "./petal-engine.js";

const PETAL_STYLES = new Set(["natural", "watercolor", "minimal"]);
const PETAL_LEVELS = new Set(["off", "low", "medium", "high"]);

export function createPetalUpdateCache(onChange, initial = { level: "off", style: "natural" }) {
  if (typeof onChange !== "function") throw new TypeError("onChange must be a function");
  let current = { level: initial.level, style: initial.style };
  return Object.freeze({
    update(settings = {}) {
      const next = {
        level: PETAL_LEVELS.has(settings.level) ? settings.level : "off",
        style: PETAL_STYLES.has(settings.style) ? settings.style : "natural",
      };
      if (next.level === current.level && next.style === current.style) return false;
      current = next;
      onChange(Object.freeze({ ...next }));
      return true;
    },
    current() { return Object.freeze({ ...current }); },
  });
}

export function installRipple(root = document) {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  function add(target, clientX, clientY) {
    if (reduced.matches || !target) return;
    const rect = target.getBoundingClientRect();
    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.left = `${(clientX ?? rect.left + rect.width / 2) - rect.left}px`;
    ripple.style.top = `${(clientY ?? rect.top + rect.height / 2) - rect.top}px`;
    target.append(ripple);
    ripple.addEventListener("animationend", () => ripple.remove(), { once: true });
  }
  function pointerdown(event) { add(event.target.closest("button, .btn"), event.clientX, event.clientY); }
  function keydown(event) {
    if (event.key === "Enter" || event.key === " ") add(event.target.closest("button, .btn"));
  }
  root.addEventListener("pointerdown", pointerdown);
  root.addEventListener("keydown", keydown);
  return Object.freeze({
    destroy() {
      root.removeEventListener("pointerdown", pointerdown);
      root.removeEventListener("keydown", keydown);
    },
  });
}

function beginPetal(context, particle) {
  context.save();
  context.translate(particle.x, particle.y);
  context.rotate(particle.rotation);
  const flip = Math.abs(particle.flip) < 0.08 ? Math.sign(particle.flip || 1) * 0.08 : particle.flip;
  context.scale(1, flip);
  context.globalAlpha = particle.opacity;
  context.filter = particle.blur > 0 ? `blur(${particle.blur}px)` : "none";
}

function petalPath(context, size, asymmetric = false, variation = 0, notched = false) {
  const shoulder = asymmetric ? size * (0.72 + variation) : size * 0.62;
  context.beginPath();
  context.moveTo(0, size * 0.58);
  const leftTipX = -size * (0.16 + variation * 0.25);
  context.bezierCurveTo(-shoulder, size * (0.24 - variation * 0.3), -size * (0.7 - variation), -size * 0.56, leftTipX, -size * 0.69);
  if (notched) {
    context.quadraticCurveTo(-size * 0.07, -size * 0.73, 0, -size * 0.53);
    context.quadraticCurveTo(size * 0.07, -size * 0.73, size * (0.17 - variation * 0.2), -size * 0.68);
  }
  context.bezierCurveTo(size * (asymmetric ? 0.18 + variation * 0.5 : 0.08), -size * 0.48, size * (0.78 + variation * 0.4), -size * 0.2, 0, size * 0.58);
  context.closePath();
}

function stableParticleSeed(particle) {
  if (Number.isFinite(particle?.gustFactor)) return Math.min(0.999999, Math.max(0, particle.gustFactor));
  const fallback = (Number(particle?.size) || 0) * 0.173 + (Number(particle?.lifetime) || 0) * 0.619;
  return ((fallback % 1) + 1) % 1;
}

function rgbaBetween(from, to, amount, alpha) {
  const channels = from.map((value, index) => Math.round(value + (to[index] - value) * amount));
  return `rgba(${channels.join(", ")}, ${alpha})`;
}

function createNaturalPalette(seed) {
  return Object.freeze({
    highlight: rgbaBetween([255, 244, 247], [255, 232, 241], seed, ".96"),
    middle: rgbaBetween([248, 190, 208], [231, 145, 178], seed, ".9"),
    edge: rgbaBetween([216, 126, 158], [180, 88, 132], seed, ".82"),
    vein: rgbaBetween([174, 98, 130], [142, 67, 105], seed, ".5"),
  });
}

const NATURAL_PALETTES = Object.freeze(
  Array.from({ length: 12 }, (_, index) => createNaturalPalette(index / 11)),
);

function naturalPaletteIndex(particle) {
  return Math.min(NATURAL_PALETTES.length - 1, Math.floor(stableParticleSeed(particle) * NATURAL_PALETTES.length));
}

export function naturalPalette(particle) {
  return NATURAL_PALETTES[naturalPaletteIndex(particle)];
}

const NATURAL_GRADIENT_CACHE = new WeakMap();

function naturalGradient(context, particle) {
  let gradients = NATURAL_GRADIENT_CACHE.get(context);
  if (!gradients) {
    gradients = new Map();
    NATURAL_GRADIENT_CACHE.set(context, gradients);
  }
  const depth = Number.isInteger(particle.depth) ? particle.depth : 1;
  const paletteIndex = naturalPaletteIndex(particle);
  const key = `${paletteIndex}:${depth}`;
  if (gradients.has(key)) return gradients.get(key);
  const extent = [7, 11, 15][depth] ?? 11;
  const palette = NATURAL_PALETTES[paletteIndex];
  const gradient = context.createLinearGradient(-extent, -extent, extent, extent);
  gradient.addColorStop(0, palette.highlight);
  gradient.addColorStop(0.58, palette.middle);
  gradient.addColorStop(1, palette.edge);
  gradients.set(key, gradient);
  return gradient;
}

export function renderNaturalPetal(context, particle) {
  beginPetal(context, particle);
  const palette = naturalPalette(particle);
  const gradient = naturalGradient(context, particle);
  context.fillStyle = gradient;
  context.shadowColor = "rgba(112, 45, 77, .24)";
  context.shadowBlur = 2 + particle.blur * 2;
  petalPath(context, particle.size, true, (stableParticleSeed(particle) - 0.5) * 0.16, true);
  context.fill();
  context.shadowBlur = 0;
  context.strokeStyle = palette.vein;
  context.lineWidth = Math.max(0.45, particle.size * 0.055);
  context.beginPath();
  context.moveTo(0, particle.size * 0.48);
  context.bezierCurveTo(-particle.size * 0.08, 0, particle.size * 0.08, -particle.size * 0.38, -particle.size * 0.04, -particle.size * 0.61);
  context.stroke();
  context.restore();
}

function paintWatercolorShape(context, size) {
  const washes = [
    [1.24, "rgba(244, 126, 169, .12)"],
    [1.04, "rgba(236, 111, 158, .18)"],
    [0.82, "rgba(255, 202, 219, .34)"],
  ];
  for (const [scale, color] of washes) {
    context.save();
    context.scale(scale, scale);
    context.fillStyle = color;
    petalPath(context, size, true);
    context.fill();
    context.restore();
  }
}

export function createWatercolorSpriteSet({
  count = 8,
  random = Math.random,
  createCanvas = () => typeof OffscreenCanvas === "function"
    ? new OffscreenCanvas(56, 56)
    : document.createElement("canvas"),
} = {}) {
  const requestedCount = Number.isFinite(count) ? Math.floor(count) : 8;
  const spriteCount = Math.max(3, Math.min(12, requestedCount));
  return Array.from({ length: spriteCount }, (_, spriteIndex) => {
    const canvas = createCanvas();
    canvas.width = 56;
    canvas.height = 56;
    const context = canvas.getContext("2d");
    context.translate(28, 28);
    const kind = spriteIndex === 0 ? "bloom" : "petal";
    if (kind === "bloom") {
      for (let petalIndex = 0; petalIndex < 5; petalIndex += 1) {
        context.save();
        context.rotate(petalIndex * Math.PI * 0.4);
        context.translate(0, -9);
        paintWatercolorShape(context, 8);
        context.restore();
      }
    } else {
      context.rotate((random() - 0.5) * 0.35);
      paintWatercolorShape(context, 14);
    }
    return Object.freeze({ image: canvas, kind });
  });
}

export function watercolorSpriteKind(particle, bloomRate = 0.065) {
  const boundedRate = Number.isFinite(bloomRate) ? Math.min(0.08, Math.max(0.05, bloomRate)) : 0.065;
  return stableParticleSeed(particle) < boundedRate ? "bloom" : "petal";
}

function selectWatercolorSprite(sprites, kind, variantSeed) {
  let candidateCount = 0;
  for (const sprite of sprites) if (sprite.kind === kind) candidateCount += 1;
  if (!candidateCount) return null;
  let target = Math.min(candidateCount - 1, Math.floor(variantSeed * candidateCount));
  for (const sprite of sprites) {
    if (sprite.kind !== kind) continue;
    if (target === 0) return sprite;
    target -= 1;
  }
  return null;
}

export function renderWatercolorPetal(context, particle, sprites) {
  if (!sprites?.length) return;
  const kind = watercolorSpriteKind(particle);
  const variantSeed = ((particle.size * 0.173 + particle.lifetime * 0.619) % 1 + 1) % 1;
  const sprite = selectWatercolorSprite(sprites, kind, variantSeed);
  if (!sprite) return;
  beginPetal(context, particle);
  const side = particle.size * 3.1;
  context.drawImage(sprite.image, -side / 2, -side / 2, side, side);
  context.restore();
}

export function renderMinimalPetal(context, particle) {
  beginPetal(context, particle);
  context.fillStyle = `rgba(190, 178, 184, ${Math.min(0.62, particle.opacity)})`;
  petalPath(context, particle.size * 0.82);
  context.fill();
  context.restore();
}

export function createPetalEffect(canvas = document.querySelector("#petalCanvas")) {
  const context = canvas?.getContext("2d");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const watercolorSprites = context ? createWatercolorSpriteSet() : [];
  let level = "off";
  let style = "natural";
  let particles = [];
  let frame = 0;
  let resizeFrame = 0;
  let previous = 0;
  let windTime = 0;
  let destroyed = false;
  let width = Math.max(1, innerWidth);
  let height = Math.max(1, innerHeight);

  function clear() { context?.clearRect(0, 0, width, height); }

  function resize() {
    if (!canvas || !context) return;
    width = Math.max(1, innerWidth);
    height = Math.max(1, innerHeight);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    syncParticles(true);
  }

  function scheduleResize() {
    if (resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      if (!destroyed) resize();
    });
  }

  function syncParticles(force = false) {
    const count = reduced.matches ? 0 : desiredCount(style, level);
    if (force || particles.length !== count) {
      particles = createParticles({ style, level, width, height });
    }
    if (!count) clear();
  }

  function render() {
    clear();
    particles.forEach((particle) => {
      if (style === "natural") renderNaturalPetal(context, particle);
      else if (style === "watercolor") renderWatercolorPetal(context, particle, watercolorSprites);
      else renderMinimalPetal(context, particle);
    });
  }

  function draw(now) {
    frame = 0;
    if (destroyed || document.hidden || reduced.matches || level === "off" || !context) return;
    const delta = Math.min((now - previous) / 1000 || 0, 0.04);
    previous = now;
    windTime += delta;
    particles = stepParticles(particles, { style, delta, windTime, width, height });
    render();
    frame = requestAnimationFrame(draw);
  }

  function start() {
    if (!frame && !document.hidden && !reduced.matches && particles.length) {
      previous = performance.now();
      frame = requestAnimationFrame(draw);
    }
  }

  function stop() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
  }

  const updateCache = createPetalUpdateCache((next) => {
    level = next.level;
    style = next.style;
    syncParticles(true);
    start();
  });

  function update(settings = {}) {
    const normalized = typeof settings === "string" ? { level: settings, style } : settings;
    return updateCache.update(normalized);
  }

  function visibilitychange() {
    if (document.hidden) stop();
    else start();
  }

  function motionChanged() {
    if (reduced.matches) {
      stop();
      particles = [];
      clear();
      return;
    }
    syncParticles(true);
    start();
  }

  resize();
  window.addEventListener("resize", scheduleResize);
  document.addEventListener("visibilitychange", visibilitychange);
  reduced.addEventListener("change", motionChanged);

  function destroy() {
    destroyed = true;
    stop();
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = 0;
    particles = [];
    clear();
    window.removeEventListener("resize", scheduleResize);
    document.removeEventListener("visibilitychange", visibilitychange);
    reduced.removeEventListener("change", motionChanged);
  }

  return Object.freeze({ update, destroy });
}

export function updatePetalEffect(controller, settings) {
  controller?.update(settings);
}

export function createEffects({ canvas = document.querySelector("#petalCanvas"), root = document } = {}) {
  const ripple = installRipple(root);
  const petals = createPetalEffect(canvas);
  return Object.freeze({
    update(settings) { updatePetalEffect(petals, settings); },
    destroy() { ripple.destroy(); petals.destroy(); },
  });
}
