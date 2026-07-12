const DEPTH_LAYERS = Object.freeze([
  Object.freeze({ size: 0.65, speed: 0.72, opacity: 0.48, blur: 1.2 }),
  Object.freeze({ size: 1, speed: 1, opacity: 0.7, blur: 0.45 }),
  Object.freeze({ size: 1.35, speed: 1.22, opacity: 0.9, blur: 0 }),
]);

export const STYLE_PROFILES = Object.freeze({
  natural: Object.freeze({ speed: 72, drift: 28, wind: 24, gust: 0.65, size: 10 }),
  watercolor: Object.freeze({ speed: 48, drift: 36, wind: 18, gust: 0.45, size: 12 }),
  minimal: Object.freeze({ speed: 38, drift: 18, wind: 12, gust: 0.2, size: 9 }),
});

const COUNTS = Object.freeze({
  natural: Object.freeze({ off: 0, low: 14, medium: 30, high: 48 }),
  watercolor: Object.freeze({ off: 0, low: 12, medium: 24, high: 38 }),
  minimal: Object.freeze({ off: 0, low: 7, medium: 14, high: 24 }),
});

function profileFor(style) {
  const profile = STYLE_PROFILES[style];
  if (!profile) throw new RangeError(`Unknown petal style: ${style}`);
  return profile;
}

export function desiredCount(style, level) {
  profileFor(style);
  const count = COUNTS[style][level];
  if (count === undefined) throw new RangeError(`Unknown petal level: ${level}`);
  return count;
}

function dimension(value, name) {
  if (!Number.isFinite(value) || value <= 0) throw new RangeError(`${name} must be finite and positive`);
  return value;
}

function sample(random) {
  const value = Number(random());
  if (!Number.isFinite(value)) return 0.5;
  return Math.min(0.999999, Math.max(0, value));
}

function makeParticle({ profile, depth, width, height, random, entering = false }) {
  const layer = DEPTH_LAYERS[depth];
  const size = profile.size * layer.size * (0.8 + sample(random) * 0.4);
  return {
    x: sample(random) * width,
    y: entering ? -size * (1 + sample(random) * 3) : sample(random) * height,
    depth,
    size,
    opacity: layer.opacity * (0.85 + sample(random) * 0.15),
    blur: layer.blur,
    vx: (sample(random) - 0.5) * profile.wind,
    vy: profile.speed * layer.speed * (0.82 + sample(random) * 0.36),
    drift: profile.drift * (0.55 + sample(random) * 0.45),
    driftPhase: sample(random) * Math.PI * 2,
    driftRate: 0.7 + sample(random) * 0.9,
    rotation: sample(random) * Math.PI * 2,
    rotationSpeed: (() => {
      const spin = sample(random);
      return (spin < 0.5 ? -1 : 1) * (0.4 + spin * 1.2);
    })(),
    flipPhase: sample(random) * Math.PI * 2,
    flipSpeed: 1.2 + sample(random) * 2.2,
    flip: 1,
    gustFactor: sample(random),
    age: 0,
    lifetime: 9 + sample(random) * 13,
  };
}

export function createParticles({ style = "natural", level = "medium", width, height, random = Math.random } = {}) {
  const profile = profileFor(style);
  const viewportWidth = dimension(width, "width");
  const viewportHeight = dimension(height, "height");
  if (typeof random !== "function") throw new TypeError("random must be a function");
  const count = desiredCount(style, level);
  return Array.from({ length: count }, (_, index) => makeParticle({
    profile,
    depth: index % DEPTH_LAYERS.length,
    width: viewportWidth,
    height: viewportHeight,
    random,
  }));
}

export function stepParticles(particles, {
  style = "natural",
  delta = 0,
  windTime = 0,
  width,
  height,
  random = Math.random,
} = {}) {
  if (!Array.isArray(particles)) throw new TypeError("particles must be an array");
  const profile = profileFor(style);
  const viewportWidth = dimension(width, "width");
  const viewportHeight = dimension(height, "height");
  if (typeof random !== "function") throw new TypeError("random must be a function");
  const dt = Number.isFinite(delta) ? Math.min(0.04, Math.max(0, delta)) : 0;
  const time = Number.isFinite(windTime) ? windTime : 0;
  const lowFrequencyWind = Math.sin(time * 0.35) * profile.wind;

  return particles.map((particle, index) => {
    const margin = Math.max(40, Number.isFinite(particle.size) ? particle.size * 4 : 40);
    if (
      !Number.isFinite(particle.x) || !Number.isFinite(particle.y) ||
      !Number.isFinite(particle.age) || !Number.isFinite(particle.lifetime) ||
      particle.age >= particle.lifetime || particle.y > viewportHeight + margin ||
      particle.x < -margin || particle.x > viewportWidth + margin
    ) {
      return makeParticle({
        profile,
        depth: Number.isInteger(particle.depth) && particle.depth >= 0 && particle.depth < 3
          ? particle.depth : index % 3,
        width: viewportWidth,
        height: viewportHeight,
        random,
        entering: true,
      });
    }

    const driftPhase = particle.driftPhase + particle.driftRate * dt;
    const gustWave = Math.max(0, Math.sin(time * 1.7 + particle.gustFactor * Math.PI * 2));
    const gust = particle.gustFactor < 0.35 ? gustWave * profile.gust * profile.wind : 0;
    const flipPhase = particle.flipPhase + particle.flipSpeed * dt;
    return {
      ...particle,
      x: particle.x + (particle.vx + lowFrequencyWind + gust + Math.sin(driftPhase) * particle.drift) * dt,
      y: particle.y + particle.vy * dt,
      driftPhase,
      rotation: particle.rotation + particle.rotationSpeed * dt,
      flipPhase,
      flip: Math.cos(flipPhase),
      age: particle.age + dt,
    };
  });
}
