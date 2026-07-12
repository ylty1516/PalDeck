import { createParticles, desiredCount, stepParticles } from "./petal-engine.js";

const PETAL_STYLES = new Set(["natural", "watercolor", "minimal"]);
const PETAL_LEVELS = new Set(["off", "low", "medium", "high"]);

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

function petalPath(context, size, asymmetric = false, variation = 0) {
  const shoulder = asymmetric ? size * (0.72 + variation) : size * 0.62;
  context.beginPath();
  context.moveTo(0, size * 0.58);
  context.bezierCurveTo(-shoulder, size * (0.24 - variation * 0.3), -size * (0.7 - variation), -size * 0.56, -size * 0.08, -size * 0.72);
  context.bezierCurveTo(size * (asymmetric ? 0.18 + variation * 0.5 : 0.08), -size * 0.48, size * (0.78 + variation * 0.4), -size * 0.2, 0, size * 0.58);
  context.closePath();
}

export function renderNaturalPetal(context, particle) {
  beginPetal(context, particle);
  const gradient = context.createLinearGradient(-particle.size, -particle.size, particle.size, particle.size);
  gradient.addColorStop(0, "rgba(255, 238, 244, .96)");
  gradient.addColorStop(0.58, "rgba(239, 154, 183, .9)");
  gradient.addColorStop(1, "rgba(180, 86, 126, .82)");
  context.fillStyle = gradient;
  context.shadowColor = "rgba(112, 45, 77, .24)";
  context.shadowBlur = 2 + particle.blur * 2;
  petalPath(context, particle.size, true, Math.sin(particle.rotation) * 0.08);
  context.fill();
  context.shadowBlur = 0;
  context.strokeStyle = "rgba(151, 70, 105, .5)";
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
  const spriteCount = Math.max(1, Math.min(12, Math.floor(count)));
  return Array.from({ length: spriteCount }, () => {
    const canvas = createCanvas();
    canvas.width = 56;
    canvas.height = 56;
    const context = canvas.getContext("2d");
    context.translate(28, 28);
    if (random() < 0.08) {
      for (let index = 0; index < 5; index += 1) {
        context.save();
        context.rotate(index * Math.PI * 0.4);
        context.translate(0, -9);
        paintWatercolorShape(context, 8);
        context.restore();
      }
    } else {
      context.rotate((random() - 0.5) * 0.35);
      paintWatercolorShape(context, 14);
    }
    return canvas;
  });
}

export function renderWatercolorPetal(context, particle, sprites, index = 0) {
  if (!sprites?.length) return;
  beginPetal(context, particle);
  const sprite = sprites[Math.abs(index) % sprites.length];
  const side = particle.size * 3.1;
  context.drawImage(sprite, -side / 2, -side / 2, side, side);
  context.restore();
}

export function minimalSafeX(x, width) {
  if (!Number.isFinite(width) || width <= 0) throw new RangeError("width must be finite and positive");
  const wrapped = ((Number.isFinite(x) ? x : 0) % width + width) % width;
  const half = width / 2;
  const edge = width * 0.2;
  return wrapped < half
    ? wrapped / half * edge
    : width - edge + (wrapped - half) / half * edge;
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
    particles.forEach((particle, index) => {
      if (style === "natural") renderNaturalPetal(context, particle);
      else if (style === "watercolor") renderWatercolorPetal(context, particle, watercolorSprites, index);
      else renderMinimalPetal(context, { ...particle, x: minimalSafeX(particle.x, width) });
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

  function update(settings = {}) {
    const nextLevel = typeof settings === "string" ? settings : settings.level;
    const nextStyle = typeof settings === "object" ? settings.style : style;
    level = PETAL_LEVELS.has(nextLevel) ? nextLevel : "off";
    style = PETAL_STYLES.has(nextStyle) ? nextStyle : "natural";
    syncParticles(true);
    start();
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
